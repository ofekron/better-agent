import { readFileSync } from "node:fs";

const source = readFileSync("src/App.tsx", "utf8");

describe("open session record restore fetches", () => {
  it("dedupes in-flight full session fetches by id", () => {
    expect(source).toContain("const openSessionRecordFetchesRef = useRef<Set<string>>(new Set())");
    expect(source).toContain("!openSessionRecordFetchesRef.current.has(id)");
    expect(source).toContain("openSessionRecordFetchesRef.current.add(id)");
    expect(source).toContain("openSessionRecordFetchesRef.current.delete(id)");

    const effectStart = source.indexOf("const idsToFetch = openSessionIds.filter(");
    const effectEnd = source.indexOf("const handleCloseTab", effectStart);
    const effectSource = source.slice(effectStart, effectEnd);
    expect(effectSource.indexOf("openSessionRecordFetchesRef.current.add(id)")).toBeLessThan(
      effectSource.indexOf('fetch(`${API}/api/sessions/summaries?${params}`'),
    );
    expect(effectSource).toContain(".finally(() => {");
  });
});
