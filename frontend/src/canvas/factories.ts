import type { OperatorSummary } from "../api/gen/types.gen";
import type {
  BuilderNode,
  CustomSpec,
  NoteSpec,
  OperatorSpec,
  PythonDecorator,
  PythonTaskSpec,
  TaskGroupSpec,
} from "../lib/types";
import { uniqueKey } from "../store/builder";

/** Starter bodies per `@task.<decorator>` (statements of the function body). */
const PYTHON_BODIES: Record<PythonDecorator, string> = {
  task: "return None",
  branch: "# Return the task_id(s) to follow.\nreturn []",
  short_circuit: "# A falsy result skips all downstream tasks.\nreturn True",
  virtualenv: "return None",
  external_python: "return None",
  branch_virtualenv: "# Return the task_id(s) to follow.\nreturn []",
  branch_external_python: "# Return the task_id(s) to follow.\nreturn []",
  sensor: "from airflow.sdk import PokeReturnValue\n\nreturn PokeReturnValue(is_done=True)",
  bash: 'return "echo hello"',
  docker: "return None",
  kubernetes: "return None",
  stub: "# Implemented by a language SDK (Java/Go/TypeScript) on the task's queue.\n...",
  llm: '# Return the prompt; the LLM answer is the task result.\nreturn "Summarize the latest report."',
  agent: '# Return the instructions; the agent may call its toolsets.\nreturn "Investigate the failing pipelines."',
  llm_branch: '# Return the prompt; the LLM picks the downstream task(s).\nreturn "Which follow-up task fits?"',
  llm_sql: '# Return the question; the LLM writes (and validates) the SQL.\nreturn "Count orders per region."',
};

export function makeOperatorSpec(op: Pick<OperatorSummary, "fqn" | "class_name">, nodes: BuilderNode[]): OperatorSpec {
  return { kind: "operator", operator: op.fqn, task_id: uniqueKey(nodes, op.class_name), params: {} };
}

export function makePythonSpec(decorator: PythonDecorator, nodes: BuilderNode[]): PythonTaskSpec {
  const key = uniqueKey(nodes, decorator === "task" ? "python" : decorator);
  return {
    kind: "python_task",
    task_id: key,
    function_name: key,
    code: PYTHON_BODIES[decorator],
    arguments: {},
    ...(decorator === "task" ? {} : { decorator }),
  };
}

export function makeGroupSpec(nodes: BuilderNode[]): TaskGroupSpec {
  return { kind: "task_group", group_id: uniqueKey(nodes, "group") };
}

export function makeNoteSpec(text = ""): NoteSpec {
  return { kind: "note", text, color: "yellow" };
}

export function makeCustomSpec(handler: string, nodes: BuilderNode[]): CustomSpec {
  return { kind: "custom", handler, task_id: uniqueKey(nodes, handler), config: {} };
}
