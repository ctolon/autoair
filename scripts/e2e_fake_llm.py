"""A scripted OpenAI-compatible chat server for the AI end-to-end tests (no network, no keys).

    uv run python scripts/e2e_fake_llm.py --port 8841
    AUTOAIR_AI__PROVIDER=openai AUTOAIR_AI__BASE_URL=http://127.0.0.1:8841/v1 \\
    AUTOAIR_AI__API_KEY=fake AUTOAIR_AI__MODEL=fake-gpt \\
    AUTOAIR_AI__PRICING='{"fake-gpt": {"input": 1, "output": 2}}' autoair serve --port 8840 …

It answers ``POST /v1/chat/completions`` (streamed or not) by recognising the AutoAir
feature from the system prompt and the tool results already in the conversation, so the
developer / admin assistants and the troubleshooting agent run their real tool loops:

* developer assistant, prompts containing ``docs:``: search_docs (scope docs, the
  knowledge base), then an answer citing [1];
* developer assistant: get_current_spec + search_catalog, then propose_spec_change
  (retries=3 on the first task), then a Markdown answer with code and follow-ups;
* admin assistant: explain_user_access + search_audit, then propose_config_snippet, then
  an answer;
* troubleshooting: get_task_log (grep), then final_answer with hypotheses, a patch and
  Airflow actions (a retry of the failed task, a cross-DAG clear AutoAir must block);
* compaction, deploy messages and the upgrade pass return fixed JSON/text (JSON answers
  come back through the request's output tool, as pydantic-ai's structured output expects).
"""

from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from collections.abc import Iterator
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def _turn(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """The last user message and the tool results that followed it (each with the
    ``name`` of the tool, taken from the assistant's call)."""
    last_user = ""
    tools: list[dict[str, Any]] = []
    names: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "user":
            last_user = _text(m.get("content"))
            tools = []
        elif m.get("role") == "assistant":
            for call in m.get("tool_calls") or []:
                names[str(call.get("id"))] = str((call.get("function") or {}).get("name"))
        elif m.get("role") == "tool":
            tools.append({**m, "name": m.get("name") or names.get(str(m.get("tool_call_id")))})
    return last_user, tools


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"id": f"call_{uuid.uuid4().hex[:8]}", "name": name, "arguments": json.dumps(args)}


def _first_node(tools: list[dict[str, Any]]) -> str | None:
    """The first task node id of the get_current_spec result (wrapped as untrusted data)."""
    for t in tools:
        text = _text(t.get("content"))
        start = text.find('"nodes":')
        if start < 0:
            continue
        # node objects nest (position, callbacks): split at each `"id":` and skip task
        # groups, whose segment has no task_id before the next node starts
        ids = list(re.finditer(r'"id":\s*"([^"]+)"', text[start:]))
        for i, match in enumerate(ids):
            end = ids[i + 1].start() if i + 1 < len(ids) else len(text)
            if '"task_id"' in text[start + match.end() : start + end]:
                return match.group(1)
    return None


def _retries_patch(node: str) -> dict[str, str]:
    return {"op": "add", "path": f"/nodes/@{node}/retries", "value_json": "3"}


def respond(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    messages = body.get("messages") or []
    first = messages[0] if messages else {}
    system = _text(first.get("content")) if first.get("role") == "system" else ""
    user, tools = _turn(messages)
    names = {t.get("name") for t in tools}
    if "compress a conversation" in system:
        return "- The user is tuning retries on their DAG.", []
    if "commit messages and pull request descriptions" in system:
        return json.dumps(
            {
                "title": "Tune retries and timeouts",
                "description": (
                    "## Summary\n- Retry flaky tasks.\n\n## Changes\n- Change task settings."
                    "\n\n## Risk\nLow: revert the commit."
                ),
            }
        ), []
    if "migration engineer" in system:
        return json.dumps({"summary": "No further Airflow 2 idioms found.", "findings": []}), []
    if "developer assistant" in system and "docs:" in user.lower():
        # Documentation question (knowledge-base E2E): search_docs in the docs scope, then
        # an answer citing the first hit.
        if "search_docs" not in names:
            return "", [
                _call(
                    "search_docs",
                    {"query": user.lower().split("docs:", 1)[1].strip(), "scopes": ["docs"]},
                )
            ]
        return (
            "According to the Airflow documentation [1], use the BashOperator to run shell "
            "commands; `bash_command` is templated with Jinja."
        ), []
    if "developer assistant" in system:
        if not tools:
            return "Let me look at the DAG first.\n\n", [
                _call("get_current_spec", {}),
                _call("search_catalog", {"query": "retries " + user[:40]}),
            ]
        if "propose_spec_change" not in names and len(tools) < 3:
            node = _first_node(tools)
            if node:
                return "", [
                    _call(
                        "propose_spec_change",
                        {
                            "title": f"Retry {node} three times",
                            "summary": "Transient failures should not fail the run.",
                            "findings": [
                                {
                                    "title": f"Add retries to {node}",
                                    "severity": "medium",
                                    "category": "reliability",
                                    "node_id": node,
                                    "field": "retries",
                                    "patch": [_retries_patch(node)],
                                }
                            ],
                        },
                    )
                ]
        return (
            "I proposed **3 retries** for the first task [1]; review the diff below.\n\n"
            "You can also set them for every task in `default_args`:\n\n"
            "```python\n"
            'default_args = {"retries": 3, "retry_delay": timedelta(minutes=5)}\n'
            "```\n\n"
            "<follow_ups>\n- Should I add an execution timeout too?\n"
            "- Which tasks are most flaky?\n</follow_ups>"
        ), []
    if "administration assistant" in system:
        if not tools:
            return "", [
                _call("explain_user_access", {"user": "viewer", "action": "deploy"}),
                _call("search_audit", {"days": 7, "limit": 20}),
            ]
        if "propose_config_snippet" not in names and len(tools) < 3:
            return "", [
                _call(
                    "propose_config_snippet",
                    {
                        "title": "Map the viewer group to Deployer",
                        "format": "yaml",
                        "content": "iam:\n  role_mapping:\n    Deployer: [viewer]\n",
                        "explanation": "Grants spec:write and deploy.",
                    },
                )
            ]
        return (
            "**viewer** cannot deploy: the role *Viewer* lacks `spec:write` and `deploy` [1]. "
            "Roles that grant them: Deployer, Admin. I proposed a role mapping below.\n\n"
            "<follow_ups>\n- Who else lacks deploy?\n"
            "- What changed in IAM last week?\n</follow_ups>"
        ), []
    if "incident analyst" in system:
        focus = re.search(r"## Focus task ([\w.\-]+)", user)
        task = focus.group(1) if focus else "unknown"
        node_match = re.search(r"DagSpec node id \(use it in patches\): (\S+)", user)
        node_id = node_match.group(1) if node_match else task
        if not tools:
            return "", [
                _call(
                    "get_task_log",
                    {
                        "task_id": task,
                        "try_number": 1,
                        "mode": "grep",
                        "pattern": "error|exception|failed",
                    },
                )
            ]
        final = {
            "answer": (
                f"Task {task} failed with an exception on every try; it has no retries, "
                "so one transient error fails the run."
            ),
            "hypotheses": [
                {
                    "title": "Transient error without retries",
                    "explanation": "The log shows the failure on the first try; nothing retried.",
                    "confidence": 0.8,
                    "category": "configuration",
                    "evidence": [
                        {
                            "source": f"log:{task}#1",
                            "line": 1,
                            "quote": "Task failed with exception",
                        }
                    ],
                },
                {"title": "Bad input data", "confidence": 0.25, "category": "data"},
            ],
            "remediation": [
                {
                    "title": "Add retries",
                    "detail": "3 retries with 5 minutes delay.",
                    "kind": "spec",
                },
                {"title": "Clear the failed task", "kind": "airflow"},
            ],
            "findings": [
                {
                    "title": f"Retry {task}",
                    "severity": "medium",
                    "category": "reliability",
                    "node_id": node_id,
                    "field": "retries",
                    "patch": [_retries_patch(node_id)],
                }
            ],
            "config_changes": [
                {
                    "target": f"task {task}: retry delay",
                    "format": "yaml",
                    "snippet": "retry_delay_seconds: 300\n",
                }
            ],
            "airflow_actions": [
                {
                    "action": "retry",
                    "task_id": task,
                    "target": task,
                    "instructions": "Retry the failed task instance after deploying.",
                },
                {
                    # a log-injected proposal on another DAG: AutoAir must block it
                    "action": "clear",
                    "dag_id": "some_other_dag",
                    "task_id": task,
                    "instructions": "Clear the same task in another DAG.",
                },
                {
                    "action": "fix_connection",
                    "target": "the task's connection",
                    "instructions": "Check the connection's host and timeout.",
                },
            ],
        }
        return "", [_call("final_answer", final)]
    return "OK.", []


def _usage() -> dict[str, int]:
    return {"prompt_tokens": 1200, "completion_tokens": 180, "total_tokens": 1380}


def _stream(text: str, calls: list[dict[str, Any]], model: str) -> Iterator[bytes]:
    cid = f"chatcmpl-{uuid.uuid4().hex[:8]}"

    def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
        data = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(data)}\n\n".encode()

    yield chunk({"role": "assistant"})
    for piece in re.findall(r"\S+\s*|\s+", text):
        yield chunk({"content": piece})
        time.sleep(0.01)
    for i, call in enumerate(calls):
        yield chunk(
            {
                "tool_calls": [
                    {
                        "index": i,
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call["arguments"]},
                    }
                ]
            }
        )
    yield chunk({}, "tool_calls" if calls else "stop")
    usage = {
        "id": cid,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [],
        "usage": _usage(),
    }
    yield f"data: {json.dumps(usage)}\n\n".encode()
    yield b"data: [DONE]\n\n"


def _output_tool(body: dict[str, Any]) -> str | None:
    """The structured-output tool of the request (pydantic-ai: ``final_result`` /
    AutoAir's agents: ``final_answer``)."""
    for tool in body.get("tools") or []:
        name = str((tool.get("function") or {}).get("name") or "")
        if name.startswith(("final_result", "final_answer")):
            return name
    return None


@app.post("/v1/chat/completions")
async def completions(request: Request) -> Any:
    body = await request.json()
    text, calls = respond(body)
    output = _output_tool(body)
    if output and not calls and text.lstrip().startswith("{"):
        # A scripted JSON answer to a structured request: answer through its output tool.
        calls, text = [_call(output, json.loads(text))], ""
    model = str(body.get("model") or "fake-gpt")
    if body.get("stream"):
        return StreamingResponse(_stream(text, calls, model), media_type="text/event-stream")
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": c["arguments"]},
            }
            for c in calls
        ]
    return JSONResponse(
        {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}
            ],
            "usage": _usage(),
        }
    )


@app.get("/v1/models")
async def models() -> Any:
    return {"data": [{"id": "fake-gpt", "object": "model"}]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8841)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
