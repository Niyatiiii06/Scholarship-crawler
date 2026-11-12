"""Downloads pages politely and turns them into clean text + links."""
import hashlib
import io
import logging
import re
import time
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("fetcher")

SKIP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".rar", ".mp4", ".mp3",
                   ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".css", ".js", ".ico", ".apk")

last_request_time = {}   # host -> time of our last request
robots_cache = {}        # host -> robots.txt rules
status_cache = {}        # url -> status code (for application link checks)


def tidy_url(url):
    """The same page can be written in several ways. Use one form for all of them."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    for port in (":80", ":443"):
        if host.endswith(port):
            host = host[:-len(port)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))


def http_get(cfg, url):
    """One HTTP GET. Returns a dictionary; never raises."""
    host = urlparse(url).netloc
    wait = cfg["crawl"]["delay_seconds"] - (time.time() - last_request_time.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    last_request_time[host] = time.time()
    try:
        response = requests.get(url, headers={"User-Agent": cfg["crawl"]["user_agent"]},
                                timeout=cfg["crawl"]["timeout_seconds"], allow_redirects=True)
    except requests.RequestException as error:
        return {"status": 0, "content_type": "", "content": b"", "text": "", "final_url": url, "error": str(error)[:200]}

    content = response.content[:3_000_000]
    encoding = response.encoding
    if not encoding or encoding.lower() == "iso-8859-1":
        encoding = response.apparent_encoding or "utf-8"
    return {"status": response.status_code, "content_type": response.headers.get("Content-Type", ""),
            "content": content, "text": content.decode(encoding, errors="replace"),
            "final_url": response.url, "error": ""}


def allowed_by_robots(cfg, url):
    parts = urlparse(url)
    host = parts.scheme + "://" + parts.netloc
    if host not in robots_cache:
        rules = robotparser.RobotFileParser()
        answer = http_get(cfg, host + "/robots.txt")
        rules.parse(answer["text"].splitlines() if answer["status"] == 200 else [])
        robots_cache[host] = rules
    return robots_cache[host].can_fetch(cfg["crawl"]["user_agent"], url)


def clean_text(text):
    text = text.replace("\xa0", " ").replace("​", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def read_pdf(content):
    """Returns (text, links) from the first pages of a PDF."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        pages = reader.pages[:8]
        text = clean_text("\n".join((page.extract_text() or "") for page in pages))
    except Exception as error:
        log.warning("could not read PDF: %s", error)
        return "", []
    links = []
    for page in pages:
        try:
            for annotation in page.get("/Annots") or []:
                target = annotation.get_object().get("/A", {}).get("/URI")
                if target and str(target).startswith("http"):
                    links.append((str(target), str(target)))
        except Exception:
            continue
    return text, links


def urls_in_text(text):
    """Web addresses written out in the text, e.g. "apply at https://scholarships.gov.in"."""
    found = []
    for url in re.findall(r"https?://[^\s<>\"')\]]+", text):
        found.append((url.rstrip(".,;"), url.rstrip(".,;")))
    for url in re.findall(r"(?<![/\w@.])www\.[a-z0-9\-]+(?:\.[a-z0-9\-]+)+(?:/[^\s<>\"')\]]*)?", text, flags=re.IGNORECASE):
        found.append(("https://" + url.rstrip(".,;"), url.rstrip(".,;")))
    return found


def read_html(html, base_url):
    """Returns (title, text, links). Links are (absolute url, link text) pairs."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    links = []
    seen = set()
    for tag in soup.find_all("a", href=True):
        href = tidy_url(urldefrag(urljoin(base_url, tag["href"].strip()))[0])
        if not href.startswith("http") or href.lower().split("?")[0].endswith(SKIP_EXTENSIONS) or href in seen:
            continue
        seen.add(href)
        links.append((href, tag.get_text(" ", strip=True)[:150]))

    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    # Prefer the main content area when the page marks one clearly
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = clean_text(main.get_text("\n"))
    if len(text) < 500 and soup.body:
        text = clean_text(soup.body.get_text("\n"))
    return title, text, links


def fetch(cfg, url):
    """Download a page and return everything the pipeline needs about it."""
    url = tidy_url(url)
    page = {"url": url, "status": 0, "title": "", "text": "", "links": [], "hash": "", "error": "", "is_pdf": False}
    try:
        if not allowed_by_robots(cfg, url):
            page["error"] = "blocked by robots.txt"
            return page
    except Exception:
        pass

    answer = http_get(cfg, url)
    page["status"] = answer["status"]
    page["error"] = answer["error"]
    if answer["status"] != 200:
        return page

    if "pdf" in answer["content_type"].lower() or url.lower().split("?")[0].endswith(".pdf"):
        page["is_pdf"] = True
        page["text"], page["links"] = read_pdf(answer["content"])
        page["title"] = page["text"].split("\n")[0][:150] if page["text"] else ""
    else:
        page["title"], page["text"], page["links"] = read_html(answer["text"], answer["final_url"])
    known = {link for link, _label in page["links"]}
    page["links"] += [(link, label) for link, label in dict(urls_in_text(page["text"])).items() if link not in known]
    page["hash"] = hashlib.sha256(page["text"].encode("utf-8")).hexdigest()
    return page


def url_status(cfg, url):
    """Status code of a link (used to check that an application link works)."""
    url = tidy_url(url)
    if url not in status_cache:
        status_cache[url] = http_get(cfg, url)["status"]
    return status_cache[url]
