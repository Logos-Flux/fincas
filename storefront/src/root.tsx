import { DEFAULT_THEME_ID, LEGACY_THEME_IDS } from "./lib/themes";
import { component$, isDev } from "@builder.io/qwik";
import { QwikCityProvider, RouterOutlet } from "@builder.io/qwik-city";
import { RouterHead } from "./components/router-head/router-head";

import "./global.css";

export default component$(() => {
  /**
   * The root of a QwikCity site always start with the <QwikCityProvider> component,
   * immediately followed by the document's <head> and <body>.
   *
   * Don't remove the `<head>` and `<body>` elements.
   */

  return (
    <QwikCityProvider>
      <head>
        <meta charset="utf-8" />
        {!isDev && (
          <link
            rel="manifest"
            href={`${import.meta.env.BASE_URL}manifest.json`}
          />
        )}
        <link
          id="theme-css"
          rel="stylesheet"
          href={`/themes/${DEFAULT_THEME_ID}.css`}
        />
        <script
          dangerouslySetInnerHTML={`
            (function() {
              try {
                var legacy = ${JSON.stringify(LEGACY_THEME_IDS)};
                var t = localStorage.getItem('golden_theme');
                if (t && legacy[t]) {
                  t = legacy[t];
                  localStorage.setItem('golden_theme', t);
                }
                if (t) document.getElementById('theme-css').href = '/themes/' + t + '.css';
              } catch(e) {}
            })();
          `}
        />
        <RouterHead />
      </head>
      <body lang="en">
        <RouterOutlet />
      </body>
    </QwikCityProvider>
  );
});
