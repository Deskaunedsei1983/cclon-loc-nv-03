"""
title: Gemma4 Reasoning Cleaner
author: local-ai-stack
version: 0.1.0
required_open_webui_version: 0.5.0
description: >
  Behebt den sichtbaren <|channel>thought ...-Leak des Gemma-Diffusion-Modells
  (vLLM-Bug vllm-project/vllm#38855: der gemma4-reasoning-parser trennt das
  reasoning_content nicht, weil skip_special_tokens die Kanal-Marker entfernt).
  Dieser OWUI-Filter raeumt im outlet die Assistant-Nachricht auf:
    - <|channel>thought ... <channel|>   ->  <think> ... </think>  (OWUI-Reasoning)
    - Harmony-Variante <|channel|>analysis ... <|channel|>final<|message|>
    - reste Marker (<|message|>, <|think|>, <|start|>, <|end|>) werden entfernt
  Installation: OWUI -> Admin -> Functions -> "+" -> Code einfuegen -> aktivieren
  und (Globe-Icon) global bzw. fuer das Modell "main"/"gemma-main" zuweisen.

  HINWEIS: An die EXAKTEN Marker deines Modells anpassen, falls noetig — die
  rohen Marker siehst du mit dem curl-Diagnosebefehl aus der README (§1b).
"""

import re
from pydantic import BaseModel, Field


# Reasoning zwischen <|channel>thought ... <channel|>  (Pipe optional, DOTALL)
_THOUGHT = re.compile(r"<\|channel\|?>thought\b\s*\n?(.*?)<\/?channel\|?>", re.DOTALL)
# Uebrig gebliebene Pipe-Marker entfernen ( <| ... >  oder  < ... |> ),
# OHNE die selbst gesetzten <think>/</think> (die haben KEINE Pipe) zu treffen:
_STRAY = re.compile(r"<\|[^>]*>|<[^<>|]*\|>")
_MULTINL = re.compile(r"\n{3,}")


def _fold(reasoning: str) -> str:
    reasoning = reasoning.strip()
    return f"<think>\n{reasoning}\n</think>\n\n" if reasoning else ""


class Filter:
    class Valves(BaseModel):
        enabled: bool = Field(default=True, description="Filter aktiv")
        keep_reasoning: bool = Field(
            default=True,
            description="Reasoning als einklappbares <think> behalten (False = ganz verwerfen)",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _clean(self, text: str) -> str:
        if not text or ("channel" not in text and "<|" not in text):
            return text

        def repl(m: "re.Match") -> str:
            return _fold(m.group(1)) if self.valves.keep_reasoning else ""

        text = _THOUGHT.sub(repl, text)
        text = _STRAY.sub("", text)                       # evtl. Marker ohne Gegenstueck
        # fuehrendes Rollen-Label, das direkt nach einem entfernten Marker stand:
        text = re.sub(r"^\s*(?:thought|analysis|final)\s*\n", "", text)
        text = _MULTINL.sub("\n\n", text)
        return text.lstrip("\n")

    async def outlet(self, body: dict, **kwargs) -> dict:
        if not self.valves.enabled:
            return body
        try:
            for msg in body.get("messages", []):
                if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
                    msg["content"] = self._clean(msg["content"])
        except Exception:
            # Im Zweifel die Antwort unveraendert lassen statt zu zerstoeren.
            pass
        return body
