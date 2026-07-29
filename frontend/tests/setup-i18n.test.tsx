import { render } from "@testing-library/react";
import { useTranslation } from "react-i18next";
import { expect, it, vi } from "vitest";

function TranslationProbe() {
  const { t } = useTranslation();
  return (
    <>
      <span>{t("missing.key")}</span>
      <span>{t("missing.default", { defaultValue: "Default text" })}</span>
    </>
  );
}

it("initializes the shared translation harness without changing missing-key semantics", () => {
  const originalConsoleWarn = console.warn.bind(console);
  const missingInstanceWarnings: string[] = [];
  const consoleWarnSpy = vi.spyOn(console, "warn").mockImplementation((...args) => {
    originalConsoleWarn(...args);
    const message = args.map(String).join(" ");
    if (message.includes("NO_I18NEXT_INSTANCE")) {
      missingInstanceWarnings.push(message);
    }
  });

  try {
    const view = render(<TranslationProbe />);
    expect(view.getByText("missing.key")).toBeTruthy();
    expect(view.getByText("Default text")).toBeTruthy();
    expect(missingInstanceWarnings).toEqual([]);
  } finally {
    consoleWarnSpy.mockRestore();
  }
});
