# house

Static placeholder website for richardstrasse.de.

## Development

Requires Bun 1.4.1 or later.

```sh
bun install --frozen-lockfile
bun run dev
bun run build
```

The build generates `src/generated/roads.ts` from the checked-in `osm_data/richardstrasse-bochum.geojson` and writes the static site to `dist/`.

## Coolify deployment

The Dockerfile follows the personal website's Bun build and Nginx runtime setup. To test locally:

```sh
docker build -t house .
docker run --rm -p 8080:80 house
```

Open http://localhost:8080. In Coolify, connect this repository and select the **Dockerfile** build pack. Set the exposed/target port to **80**, not 3000; no host port mapping is needed. Configure `https://richardstrasse.de` as the domain and let Coolify manage HTTPS.

For the cutover, first deploy and test using a temporary Coolify domain. Then point the domain's DNS A record to the Coolify VPS, and update or remove any AAAA record to match the VPS's IPv6 configuration. Verify DNS, HTTPS and the site before disabling GitHub Pages. The `CNAME` file is retained for the existing Pages deployment until cutover; it is not used by the container.
