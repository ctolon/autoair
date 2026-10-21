import { ChakraProvider, defaultSystem } from "@chakra-ui/react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";

const root = document.getElementById("root") as HTMLElement;
document.documentElement.style.height = "100%";
document.body.style.height = "100%";
document.body.style.margin = "0";
root.style.height = "100vh";

createRoot(root).render(
  <StrictMode>
    <ChakraProvider value={defaultSystem}>
      <App />
    </ChakraProvider>
  </StrictMode>,
);
