import { describe, expect, it } from "vitest";
import { formatPrice } from "../../src/lib/format-price";

describe("formatPrice", () => {
  it("formats USD in en-US", () => {
    expect(formatPrice("1234.5", "USD", "en-US")).toBe("$1,234.50");
  });

  it("formats EUR in de-DE with comma decimals", () => {
    expect(formatPrice("1234.5", "EUR", "de-DE")).toContain("1.234,50");
    expect(formatPrice("1234.5", "EUR", "de-DE")).toContain("€");
  });

  it("formats JPY with no decimal places", () => {
    expect(formatPrice("1500", "JPY", "en-US")).toBe("¥1,500");
  });

  it("formats GBP in en-GB", () => {
    expect(formatPrice("99.99", "GBP", "en-GB")).toBe("£99.99");
  });

  it("accepts numeric input and defaults to USD", () => {
    expect(formatPrice(42, "USD", "en-US")).toBe("$42.00");
  });

  it("still shows code and amount for unknown-but-wellformed currency codes", () => {
    const rendered = formatPrice("10.00", "XXQ", "en-US");
    expect(rendered).toContain("XXQ");
    expect(rendered).toContain("10.00");
  });

  it("falls back to a plain amount+code string for malformed currency codes", () => {
    expect(formatPrice("10.00", "not-a-currency", "en-US")).toBe(
      "10.00 not-a-currency",
    );
  });

  it("falls back gracefully for non-numeric amounts", () => {
    expect(formatPrice("n/a", "USD", "en-US")).toBe("n/a USD");
  });
});
