export type CodeLanguage =
  | "jinja"
  | "python"
  | "bash"
  | "sql"
  | "yaml"
  | "json"
  | "markdown"
  | "go"
  | "java"
  | "javascript"
  | "typescript"
  | "text";

/** Data the Jinja completion source offers (gathered by the caller from the store / API). */
export type JinjaCompletionData = {
  /** Declared DAG params. */
  params: { name: string; detail?: string | undefined }[];
  /** Airflow Variable keys. */
  variables: string[];
  /** Airflow connection ids. */
  connections: { id: string; type?: string | null | undefined }[];
  /** Upstream tasks (effective task ids) and their known output keys. */
  upstream: { taskId: string; keys: string[] }[];
};

export type CodeEditorHandle = {
  /** Replaces the selection (or inserts at the cursor) and focuses the editor. */
  insert: (text: string) => void;
  focus: () => void;
  /**
   * Replaces a range computed from the document and the selection, then selects
   * `selectionFrom..selectionTo` (toolbar commands such as Markdown formatting).
   */
  edit: (
    fn: (
      doc: string,
      from: number,
      to: number,
    ) => { from: number; to: number; insert: string; selectionFrom: number; selectionTo: number },
  ) => void;
};

export type CodeEditorProps = {
  value: string;
  onChange: (v: string) => void;
  language: CodeLanguage;
  /** Enables `{{ … }}` highlighting and Airflow-context autocompletion. */
  jinja?: JinjaCompletionData | null | undefined;
  /** Accessible name of the editor. */
  ariaLabel: string;
  minHeight?: string | undefined;
  maxHeight?: string | undefined;
  /** Fixed height (set when the user resizes the editor; overrides min/max). */
  height?: string | undefined;
  readOnly?: boolean | undefined;
  placeholder?: string | undefined;
  invalid?: boolean | undefined;
  /** Single line feel (no line numbers / gutters); still allows newlines. */
  compact?: boolean | undefined;
  onFocus?: ((handle: CodeEditorHandle) => void) | undefined;
  onBlur?: (() => void) | undefined;
  /** Called once the editor exists (toolbars drive it through the handle). */
  onReady?: ((handle: CodeEditorHandle) => void) | undefined;
  /** Server-side lint results (1-based lines) shown as gutter markers and underlines. */
  diagnostics?: CodeDiagnostic[] | undefined;
  /** Hide the resize handle and the "open in a large editor" button. */
  noExpand?: boolean | undefined;
  /** Extra controls shown above the editor in the large dialog (e.g. the Jinja "Insert" menu). */
  expandToolbar?: import("react").ReactNode;
  /** Shown under the editor in the large dialog (e.g. the rendered template preview). */
  expandFooter?: import("react").ReactNode;
  /** Title of the large editor dialog (defaults to the aria label). */
  expandTitle?: string | undefined;
  /** Hide the "Validate" button (server-side syntax/lint check of the code). */
  noValidate?: boolean | undefined;
  /** Python language services (completion, hover) for a task body. */
  pythonLsp?: PythonLspContext | null | undefined;
  /** Internal (LazyEditor): focus and selection of the textarea shown while CodeMirror loads. */
  focusHandoffRef?: import("react").RefObject<FocusHandoff | null> | undefined;
};

/** Where the user was typing in the loading placeholder (handed over to CodeMirror). */
export type FocusHandoff = { anchor: number; head: number };

export type PythonLspContext = {
  functionName: string;
  arguments: string[];
  usesLoopContext: boolean;
  /** "module": the editor holds a whole module (inline function, .py file), not a task body. */
  mode?: "task_body" | "module";
};

export type CodeDiagnostic = {
  line: number;
  column?: number | null | undefined;
  message: string;
  severity: "error" | "warning" | "info";
};
