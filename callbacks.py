"""Callback de logging (BaseCallbackHandler): registra cada chamada de ferramenta e de modelo do agente.

Por que um callback e não prints no código das ferramentas: o callback observa o agente de fora, recebe os
eventos do próprio LangChain (início, fim e erro de ferramenta e de LLM) e serve para qualquer ferramenta
nova sem alterá-la. O log vai para evidencias/log_agente.jsonl (uma linha por evento) e alimenta o relatório.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

import config

CAMINHO_LOG = config.RAIZ / "evidencias" / "log_agente.jsonl"


class LogAgente(BaseCallbackHandler):
    def __init__(self, caminho: Path | None = None, sessao: str = "", eco: bool = False):
        self.caminho, self.sessao, self.eco = Path(caminho or CAMINHO_LOG), sessao, eco
        self.inicio: dict[UUID, float] = {}
        self.eventos: list[dict] = []           # cópia em memória (útil no notebook)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)

    def _gravar(self, evento: dict) -> None:
        evento = {"quando": datetime.now().isoformat(timespec="seconds"), "sessao": self.sessao, **evento}
        self.eventos.append(evento)
        with open(self.caminho, "a", encoding="utf-8") as f:
            f.write(json.dumps(evento, ensure_ascii=False, default=str) + "\n")
        if self.eco:
            print(f"  [log] {evento['evento']}: {evento.get('ferramenta', '')} {evento.get('segundos', '')}")

    def on_tool_start(self, serialized, input_str, *, run_id, **kw):
        self.inicio[run_id] = time.perf_counter()
        self._gravar({"evento": "ferramenta_inicio", "ferramenta": (serialized or {}).get("name"),
                      "entrada": str(input_str)[:300]})

    def on_tool_end(self, output, *, run_id, **kw):
        dur = round(time.perf_counter() - self.inicio.pop(run_id, time.perf_counter()), 2)
        self._gravar({"evento": "ferramenta_fim", "ferramenta": kw.get("name"), "segundos": dur,
                      "saida": str(getattr(output, "content", output))[:300]})

    def on_tool_error(self, error, *, run_id, **kw):
        self.inicio.pop(run_id, None)
        self._gravar({"evento": "ferramenta_erro", "ferramenta": kw.get("name"), "erro": repr(error)[:300]})

    def on_chat_model_start(self, serialized, messages, *, run_id, **kw):
        self.inicio[run_id] = time.perf_counter()

    def on_llm_end(self, response, *, run_id, **kw):
        dur = round(time.perf_counter() - self.inicio.pop(run_id, time.perf_counter()), 2)
        uso = (response.llm_output or {}).get("token_usage", {}) if hasattr(response, "llm_output") else {}
        self._gravar({"evento": "llm_fim", "segundos": dur, "tokens": uso})

    def on_llm_error(self, error, *, run_id, **kw):
        self.inicio.pop(run_id, None)
        self._gravar({"evento": "llm_erro", "erro": repr(error)[:300]})
