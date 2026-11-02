> **AI disclosure:** Approximately 90% of this project was coded by AI under close human guidance.

# richardstrasse.de

Static placeholder website for richardstrasse.de.

## Development

Requires Bun 1.4.1 or later.

```sh
bun install --frozen-lockfile
bun run dev # start the development server
bun run build # or build the production bundle into dist/index.html
```

To update the HTML's timestamp automatically on each commit, enable the repository's hook once:

```sh
git config core.hooksPath .githooks
```

The hook records UTC date and time and stages `src/index.html`. Stage or stash any changes to that file before committing so the hook cannot include unstaged edits accidentally.

## Map data and build

The map data comes from a regional OpenStreetMap PBF extract. `draw_richardstrasse.py` reads the local extract, finds Richardstraße near Bochum, and exports nearby roads and railways to the checked-in GeoJSON, omitting unnecessary metadata. 

`scripts/generate-roads.ts` gzips that GeoJSON and encodes it as base64 in `src/generated/roads.ts`. Bun embeds it in the HTML, and the browser decompresses it before drawing the map. This keeps the map data compact and avoids a separate data request, reducing the downloaded page size.

As defined in `package.json`, both `bun run dev` and `bun run build` always run the generate step first. The build then bundles and minifies the site into a single self-contained `dist/index.html`; see the [Bun standalone HTML documentation](https://bun.com/docs/bundler/standalone-html).
