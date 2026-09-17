import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { initTheme } from "./utils/theme";
import "@fontsource-variable/manrope";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "@fontsource/ibm-plex-mono/600.css";
import "./styles/tokens.css";
import "./styles/fonts.css";
import "./styles/base.css";
import { App } from "./App";

const rootElement = document.getElementById("root");
if (rootElement === null) throw new Error("missing #root element");

initTheme();
createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
