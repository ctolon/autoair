import { createContext, useContext } from "react";

/**
 * The node being edited by the surrounding inspector (null in DAG settings). Editors use
 * it to offer upstream task outputs (XCom), branch targets, and context-aware completion.
 */
export const EditingNodeContext = createContext<string | null>(null);

export const useEditingNode = (): string | null => useContext(EditingNodeContext);
