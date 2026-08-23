# Third-party notices

Decky Controller itself is released under the MIT License (see `LICENSE`). The components below are
either partially derived from third-party work or were used as a reference; their licenses and
attributions are reproduced here as required.

## decky-plugin-template (BSD-3-Clause)

The project scaffold — `package.json` (scripts, dependency set, `pnpm.peerDependencyRules`),
`rollup.config.js`, `tsconfig.json` and the `src/` entry-point structure (`src/index.tsx` with
`definePlugin`) — is derived from the official Decky Loader plugin template:

* Repository: <https://github.com/SteamDeckHomebrew/decky-plugin-template>
* Copyright (c) 2022-2024, Steam Deck Homebrew
* License: BSD 3-Clause (full text below, as published in the template's `LICENSE` file; the template's
  placeholder line "Copyright (c) 2024, Hypothetical Plugin Developer", which the template asks each
  plugin author to replace, is intentionally omitted)

All plugin-specific code (the backend `main.py`, the daemon in `py_modules/deckgadget/`, the UI in
`src/*.tsx` / `src/*.ts`, tests and documentation) is original work under the MIT License.

```
BSD 3-Clause License

Copyright (c) 2022-2024, Steam Deck Homebrew

All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## SDL — Simple DirectMedia Layer (zlib) — protocol reference only

The layout of the Steam Deck built-in controller's HID input report and the feature-report commands used
to take over / release it (`py_modules/deckgadget/sources/neptune/protocol.py` and `commands.py` — constants marked `# SDL:`)
were worked out with SDL's Steam Deck HIDAPI driver as a reference for the wire protocol.
**No SDL source code is copied into this project**; only the publicly observable protocol (report IDs,
byte offsets, message/command numbers) was used, and the Python implementation is original.

* Repository: <https://github.com/libsdl-org/SDL> (`src/joystick/hidapi/SDL_hidapi_steamdeck.c`,
  `src/joystick/hidapi/steam/controller_structs.h`, `src/joystick/hidapi/steam/controller_constants.h`)
* Copyright (C) 1997-2026 Sam Lantinga
* License: zlib — <https://github.com/libsdl-org/SDL/blob/main/LICENSE.txt>

## Runtime dependencies bundled into `dist/index.js`

The frontend bundle produced by rollup inlines the following npm packages (their licenses permit
redistribution in bundled form; see each package's `LICENSE` in `node_modules/` after `pnpm install`):

| package | license |
|---|---|
| `@decky/api`, `@decky/ui` | LGPL-2.1 (Decky Loader, Steam Deck Homebrew) |
| `@decky/rollup` (build tool only, not bundled) | BSD-3-Clause |
| `react-icons` | MIT (icons themselves carry their upstream icon-set licenses) |
| `tslib` | 0BSD |

`react` / `react-dom` are not bundled — they are provided at runtime by the Steam client via Decky Loader.
