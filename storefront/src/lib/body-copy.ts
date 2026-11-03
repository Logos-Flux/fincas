/**
 * Render a category's `body_copy` (ERP-authored, plain markdown) to HTML.
 *
 * Supports exactly what the ERP's help text promises: paragraphs, `##` /
 * `###` headings, `- ` lists, `**bold**` and `[links](/relative/url/)`.
 * Everything is HTML-escaped first, so copy can never inject markup; links
 * are kept only when they are site-relative or http(s).
 */

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function safeHref(href: string): string | null {
  const h = href.trim();
  if (/^\/(?!\/)/.test(h)) return h;
  if (/^https?:\/\//i.test(h)) return h;
  return null;
}

function inline(text: string): string {
  let html = escapeHtml(text);
  html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, label: string, href: string) => {
    // href was escaped with the rest; undo &amp; so query strings survive.
    const target = safeHref(href.replace(/&amp;/g, "&"));
    return target ? `<a href="${escapeHtml(target)}">${label}</a>` : label;
  });
  return html;
}

export function renderBodyCopy(markdown: string | null | undefined): string {
  if (!markdown || !markdown.trim()) return "";
  const blocks = markdown.replace(/\r\n?/g, "\n").split(/\n\s*\n/);
  const out: string[] = [];
  for (const block of blocks) {
    const lines = block.split("\n").filter((l) => l.trim());
    if (lines.length === 0) continue;
    let paragraph: string[] = [];
    let list: string[] = [];
    const flushParagraph = () => {
      if (paragraph.length) out.push(`<p>${inline(paragraph.join(" "))}</p>`);
      paragraph = [];
    };
    const flushList = () => {
      if (list.length) out.push(`<ul>${list.map((i) => `<li>${inline(i)}</li>`).join("")}</ul>`);
      list = [];
    };
    for (const raw of lines) {
      const line = raw.trim();
      const heading = /^(#{2,3})\s+(.*)$/.exec(line);
      if (heading) {
        flushParagraph();
        flushList();
        const tag = heading[1].length === 2 ? "h2" : "h3";
        out.push(`<${tag}>${inline(heading[2])}</${tag}>`);
      } else if (/^[-*]\s+/.test(line)) {
        flushParagraph();
        list.push(line.replace(/^[-*]\s+/, ""));
      } else {
        flushList();
        paragraph.push(line);
      }
    }
    flushParagraph();
    flushList();
  }
  return out.join("\n");
}
