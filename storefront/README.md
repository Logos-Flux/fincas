# Qwik City App ⚡️

- [Qwik Docs](https://qwik.dev/)
- [Discord](https://qwik.dev/chat)
- [Qwik GitHub](https://github.com/QwikDev/qwik)
- [@QwikDev](https://twitter.com/QwikDev)
- [Vite](https://vitejs.dev/)

---

## Project Structure

This project is using Qwik with [QwikCity](https://qwik.dev/qwikcity/overview/). QwikCity is just an extra set of tools on top of Qwik to make it easier to build a full site, including directory-based routing, layouts, and more.

Inside your project, you'll see the following directory structure:

```
├── public/
│   └── ...
└── src/
    ├── components/
    │   └── ...
    └── routes/
        └── ...
```

- `src/routes`: Provides the directory-based routing, which can include a hierarchy of `layout.tsx` layout files, and an `index.tsx` file as the page. Additionally, `index.ts` files are endpoints. Please see the [routing docs](https://qwik.dev/qwikcity/routing/overview/) for more info.

- `src/components`: Recommended directory for components.

- `public`: Any static assets, like images, can be placed in the public directory. Please see the [Vite public directory](https://vitejs.dev/guide/assets.html#the-public-directory) for more info.

## Catalog pages and SEO

Category pages live at `/c/<slug>/`, brands at `/brands/` and
`/brands/<slug>/`. Every catalog page builds its head with
`src/lib/seo-head.ts`: titles and descriptions come from the ERP's `seo_*`
fields, with neutral fallbacks (`<name> | <VITE_SITE_NAME>`, a whole-sentence
summary). Set `VITE_SITE_NAME` and `VITE_SITE_URL` per deployment; never put a
store name or SEO copy in storefront code. Full contract:
`docs/storefront-seo.md`.

## Customer accounts (`/account`)

Signed-in customers get an account area: `/account/login`, `/account`
(profile + totals), `/account/orders` and `/account/orders/<id>`. It reads
the ERP's customer portal API (`/api/v1/portal/customer/…`, see
`docs/identity-unification.md` §5) with `Authorization: Bearer <access token>`.

Auth goes through a small adapter in `src/lib/auth/` (`getSession()`,
`signIn(email, password)`, `signOut()`, `getAccessToken()`), so the pages do
not know which identity provider is behind it. Which implementation runs:

| Setting                                                             | Result                                                                                                                                                              |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `VITE_SUPABASE_URL` + `VITE_SUPABASE_ANON_KEY`                      | Supabase email + password. The SDK loads only when `/account` is used.                                                                                              |
| neither, on the dev server (and e2e)                                | **mock**: any email + password signs in (except `wrong-password`, which exercises the error state); the token is fake, so it only works against a mocked portal API |
| neither, in a production build                                      | **unconfigured**: sign-in says accounts are not set up; nothing is faked                                                                                            |
| `VITE_AUTH_PROVIDER=mock`                                           | mock, even in a production build (demos)                                                                                                                            |
| `VITE_AUTH_PROVIDER=supabase` without both vars, or any other value | unconfigured                                                                                                                                                        |

Sessions live in memory only (`persistSession: false`, with the SDK's token
refresh); nothing is written to `localStorage`. A full page reload therefore
signs the customer out. The ERP must list the storefront origin in
`CORS_ALLOWED_ORIGINS` and share the Supabase project's JWT secret
(`SUPABASE_JWT_SECRET`).

The portal client (`src/lib/portal-client.ts`) never invents data: a failed
call shows an error state, and a 401/403 signs the customer out and sends
them back to `/account/login?next=…`. `tests/e2e/account.spec.ts` mocks the
portal API at `E2E_API_ORIGIN` like the catalog journeys.

## Add Integrations and deployment

Use the `bun qwik add` command to add additional integrations. Some examples of integrations includes: Cloudflare, Netlify or Express Server, and the [Static Site Generator (SSG)](https://qwik.dev/qwikcity/guides/static-site-generation/).

```shell
bun qwik add # or `bun qwik add`
```

## Development

Development mode uses [Vite's development server](https://vitejs.dev/). The `dev` command will server-side render (SSR) the output during development.

```shell
npm start # or `bun start`
```

> Note: during dev mode, Vite may request a significant number of `.js` files. This does not represent a Qwik production build.

## Preview

The preview command will create a production build of the client modules, a production build of `src/entry.preview.tsx`, and run a local server. The preview server is only for convenience to preview a production build locally and should not be used as a production server.

```shell
bun preview # or `bun preview`
```

## Production

The production build will generate client and server modules by running both client and server build commands. The build command will use Typescript to run a type check on the source code.

```shell
bun build # or `bun build`
```
