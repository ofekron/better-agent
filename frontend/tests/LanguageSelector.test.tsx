import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import type { ReactNode } from "react";

const { tMock, changeLanguageMock, trackedFetchMock } = vi.hoisted(() => ({
  tMock: vi.fn((key: string, fallback?: string) => fallback ?? key),
  changeLanguageMock: vi.fn<(lng: string) => Promise<void>>(async () => {}),
  trackedFetchMock: vi.fn(
    async (
      _label: string,
      _url: string,
      _init?: unknown,
      _opts?: unknown,
    ): Promise<unknown> => ({ ok: true }),
  ),
}));

// i18n exposes the current language plus changeLanguage; t() returns its
// fallback so the aria-label is observable without a real resource bundle.
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: tMock,
    i18n: { language: "en", changeLanguage: changeLanguageMock },
  }),
}));
vi.mock("../src/api", () => ({ API: "/test-api" }));
vi.mock("../src/progress/store", () => ({ trackedFetch: trackedFetchMock }));

// Native-select stub: drives onChange directly, exposes value/options.
vi.mock("../src/components/Select", () => ({
  Select: ({
    value,
    options,
    onChange,
    "aria-label": ariaLabel,
  }: {
    value: string;
    options: { value: string; label: ReactNode }[];
    onChange: (v: string) => void;
    "aria-label"?: string;
  }) => (
    <select
      data-testid="lang-select"
      aria-label={ariaLabel}
      value={value}
      onChange={(e) => onChange((e.target as HTMLSelectElement).value)}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {String(o.label)}
        </option>
      ))}
    </select>
  ),
}));

import { LanguageSelector } from "../src/components/LanguageSelector";

function selectEl(container: HTMLElement): HTMLSelectElement {
  return container.querySelector(
    'select[data-testid="lang-select"]',
  ) as HTMLSelectElement;
}

beforeEach(() => {
  tMock.mockClear();
  changeLanguageMock.mockClear();
  trackedFetchMock.mockClear();
  trackedFetchMock.mockResolvedValue({ ok: true });
});

describe("LanguageSelector", () => {
  it("renders the current language, all autonym options, and an translated aria-label", () => {
    const { container } = render(<LanguageSelector />);

    const select = selectEl(container);
    expect(select.value).toBe("en");
    expect(select.getAttribute("aria-label")).toBe("Language");
    expect(tMock).toHaveBeenCalledWith("language.label", "Language");

    const codes = [...select.querySelectorAll("option")].map((o) =>
      (o as HTMLOptionElement).value,
    );
    expect(codes).toEqual([
      "en", "he", "es", "fr", "de", "pt", "it", "ru",
      "zh", "ja", "ko", "ar", "hi", "nl",
    ]);
    // Autonyms render verbatim (not run through t()).
    expect(
      (select.querySelector('option[value="he"]') as HTMLOptionElement).textContent,
    ).toBe("עברית");
  });

  it("switches language and PATCHes the preference via trackedFetch (silent)", () => {
    const { container } = render(<LanguageSelector />);
    fireEvent.change(selectEl(container), { target: { value: "he" } });

    expect(changeLanguageMock).toHaveBeenCalledWith("he");
    expect(trackedFetchMock).toHaveBeenCalledWith(
      "language:save",
      "/test-api/api/user-prefs",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ language: "he" }),
      },
      { silent: true },
    );
  });

  it("swallows a failed save without throwing (catch no-op)", async () => {
    trackedFetchMock.mockRejectedValue(new Error("network"));
    const { container } = render(<LanguageSelector />);
    fireEvent.change(selectEl(container), { target: { value: "fr" } });

    // Drain the microtask queue so the rejected promise's .catch runs inside
    // the test rather than surfacing as an unhandled rejection later.
    await Promise.resolve();
    await Promise.resolve();

    expect(changeLanguageMock).toHaveBeenCalledWith("fr");
    expect(trackedFetchMock).toHaveBeenCalled();
  });
});
