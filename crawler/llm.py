"""Asks a free LLM (Groq, Gemini, or local Ollama) and returns its JSON answer.

Free tiers have small limits, so when one model is exhausted we move to the next.
"""
import json
import logging
import os
import re
import time

import requests

log = logging.getLogger("llm")

GROQ_URL = "https://api.groq.com/openai/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
OLLAMA_URL = "http://localhost:11434"

state = {"last_call": 0, "exhausted": set(), "available": {}, "reasoning_setting": True}


class LLMError(Exception):
    pass


def api_key(provider):
    return {"groq": os.getenv("GROQ_API_KEY"), "gemini": os.getenv("GEMINI_API_KEY"), "ollama": "local"}.get(provider)


def is_available(cfg):
    for provider in cfg["llm"]["providers"]:
        if provider == "ollama":
            try:
                if requests.get(OLLAMA_URL + "/api/tags", timeout=2).status_code == 200:
                    return True
            except requests.RequestException:
                continue
        elif api_key(provider):
            return True
    return False


def models_for(cfg, provider, key):
    """Models from config.yaml that this key can really use (checked once per run)."""
    if provider in state["available"]:
        return state["available"][provider]
    wanted = cfg["llm"]["models"][provider]
    try:
        if provider == "groq":
            data = requests.get(GROQ_URL + "/models", headers={"Authorization": "Bearer " + key}, timeout=20).json()
            existing = [m["id"] for m in data.get("data", [])]
        elif provider == "gemini":
            data = requests.get(GEMINI_URL + "/models", params={"pageSize": 200}, headers={"x-goog-api-key": key}, timeout=20).json()
            existing = [m["name"].replace("models/", "") for m in data.get("models", [])]
        else:
            data = requests.get(OLLAMA_URL + "/api/tags", timeout=5).json()
            existing = [m["name"].split(":")[0] for m in data.get("models", [])]
        usable = [m for m in wanted if m in existing] or wanted
    except Exception:
        usable = wanted
    state["available"][provider] = usable
    return usable


def send(provider, key, model, system, user):
    """One request. Returns the HTTP response."""
    if provider == "groq":
        body = {"model": model, "temperature": 0, "max_tokens": 4000, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        # Reasoning models spend answer tokens on thinking; ask for little of it so the JSON is not cut off
        if "gpt-oss" in model and state["reasoning_setting"]:
            body["reasoning_effort"] = "low"
        return requests.post(GROQ_URL + "/chat/completions", headers={"Authorization": "Bearer " + key}, json=body, timeout=90)
    if provider == "gemini":
        body = {"system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 8000, "responseMimeType": "application/json"}}
        return requests.post(GEMINI_URL + "/models/" + model + ":generateContent", headers={"x-goog-api-key": key}, json=body, timeout=90)
    body = {"model": model, "stream": False, "format": "json", "options": {"temperature": 0},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    return requests.post(OLLAMA_URL + "/api/chat", json=body, timeout=300)


def read_answer(provider, data):
    try:
        if provider == "groq":
            return data["choices"][0]["message"]["content"]
        if provider == "gemini":
            return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
        return data["message"]["content"]
    except (KeyError, IndexError):
        raise LLMError("unexpected response: " + str(data)[:200])


def read_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    raise LLMError("model did not return valid JSON")


def ask_llm(cfg, system, user):
    """Returns (answer dictionary, "provider/model"). Raises LLMError if every model fails."""
    last_error = "no LLM provider configured"
    for provider in cfg["llm"]["providers"]:
        key = api_key(provider)
        if not key:
            continue
        for model in models_for(cfg, provider, key):
            label = provider + "/" + model
            if label in state["exhausted"]:
                continue
            for attempt in range(1, 4):
                wait = cfg["llm"]["seconds_between_calls"] - (time.time() - state["last_call"])
                if wait > 0:
                    time.sleep(wait)
                state["last_call"] = time.time()
                try:
                    response = send(provider, key, model, system, user)
                except requests.RequestException as error:
                    last_error = label + ": " + str(error)[:150]
                    break
                if response.status_code == 200:
                    try:
                        return read_json(read_answer(provider, response.json())), label
                    except LLMError as error:
                        last_error = label + ": " + str(error)
                        continue
                last_error = label + ": HTTP " + str(response.status_code) + " " + response.text[:200].replace("\n", " ")
                if response.status_code == 400 and "reasoning" in response.text.lower() and state["reasoning_setting"]:
                    state["reasoning_setting"] = False   # this model does not accept the setting: retry without it
                    continue
                if response.status_code == 429:
                    # Rate limit: wait if it is short, otherwise give this model up for the run
                    try:
                        retry_after = float(response.headers.get("retry-after", "30"))
                    except ValueError:
                        retry_after = 30
                    if retry_after <= 90 and attempt < 3:
                        log.info("%s rate limited, waiting %ds", label, retry_after + 1)
                        time.sleep(retry_after + 1)
                        continue
                    # A long wait means the daily limit is used up: skip this model for the rest of the run
                    state["exhausted"].add(label)
                    break
                if response.status_code in (500, 502, 503, 504) and attempt < 3:
                    time.sleep(5 * attempt)
                    continue
                if response.status_code in (401, 403, 404):
                    # Wrong key or model does not exist: no point trying it again
                    state["exhausted"].add(label)
                break
            log.warning("%s failed (%s); trying the next model", label, last_error)
    raise LLMError(last_error)


def self_test(cfg):
    """Used by `python main.py check`: tries every configured model once and prints what happened."""
    found_key = False
    for provider in cfg["llm"]["providers"]:
        key = api_key(provider)
        if not key:
            print(provider + ": no API key in .env - skipped")
            continue
        found_key = True
        for model in models_for(cfg, provider, key):
            try:
                response = send(provider, key, model, "Reply in JSON.", 'Return the JSON object {"ok": true}')
            except requests.RequestException as error:
                print(provider + "/" + model + ": could not connect (" + str(error)[:120] + ")")
                continue
            if response.status_code == 200:
                print(provider + "/" + model + ": OK")
            else:
                print(provider + "/" + model + ": HTTP " + str(response.status_code) + " " + response.text[:200].replace("\n", " "))
            time.sleep(2)
    return found_key
