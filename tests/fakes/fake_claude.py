#!/usr/bin/env python3
"""Claude Code falso: devolve respostas de uma fila em $FAKE_CLAUDE_QUEUE e registra argumentos."""
import json
import os
import sys

queue_path = os.environ["FAKE_CLAUDE_QUEUE"]
log_path = os.environ.get("FAKE_CLAUDE_LOG")
if log_path:
    with open(log_path, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")
with open(queue_path) as f:
    queue = json.load(f)
item = queue.pop(0) if queue else {"exit": 1, "stderr": "fila vazia"}
with open(queue_path, "w") as f:
    json.dump(queue, f)
if item.get("stderr"):
    sys.stderr.write(item["stderr"])
if "stdout" in item:
    sys.stdout.write(item["stdout"] if isinstance(item["stdout"], str) else json.dumps(item["stdout"]))
sys.exit(item.get("exit", 0))
