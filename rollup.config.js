import deckyPlugin from "@decky/rollup";

// @decky/rollup already wires TypeScript, JSON, image (data-URI) imports,
// externalizes react/react-dom (provided by the Steam client) and emits dist/index.js.
export default deckyPlugin({
  // Add extra Rollup options here if ever needed.
});
