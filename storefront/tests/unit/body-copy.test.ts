import { describe, expect, it } from "vitest";
import { renderBodyCopy } from "../../src/lib/body-copy";

describe("renderBodyCopy", () => {
  it("renders the markdown subset the ERP promises", () => {
    const html = renderBodyCopy(
      "## About\n\nHardware for **real** installs. See [ground mounts](/c/ground-mounts/).\n\n### Kinds\n- Roof\n- Ground",
    );
    expect(html).toBe(
      [
        "<h2>About</h2>",
        '<p>Hardware for <strong>real</strong> installs. See <a href="/c/ground-mounts/">ground mounts</a>.</p>',
        "<h3>Kinds</h3>",
        "<ul><li>Roof</li><li>Ground</li></ul>",
      ].join("\n"),
    );
  });

  it("escapes HTML and drops unsafe links", () => {
    const html = renderBodyCopy(
      "<script>alert(1)</script> [x](javascript:alert(1)) [y](//evil.example)",
    );
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("href");
  });

  it("is empty for empty copy", () => {
    expect(renderBodyCopy(null)).toBe("");
    expect(renderBodyCopy("  \n ")).toBe("");
  });
});
