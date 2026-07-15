import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";
import { frontendBackendMutationExclusions } from "../../src/sync/frontendBackendMutationCoverage";

const methods = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function sourceFiles(root: string): string[] {
  return readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const path = join(root, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return /\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

function mutations(file: string): Array<{ line: number; route: string; method: string; canonical: boolean }> {
  const text = readFileSync(file, "utf8");
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true,
    file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const found: Array<{ line: number; route: string; method: string; canonical: boolean }> = [];
  const hasCanonicalAncestor = (node: ts.Node): boolean => {
    for (let current: ts.Node | undefined = node; current; current = current.parent) {
      if (!ts.isCallExpression(current)) continue;
      const callee = current.expression.getText(source);
      if (callee === "runThreeStateSync" || callee === "runMutation" || callee === "runBusyAction") return true;
    }
    return false;
  };
  const visit = (node: ts.Node): void => {
    if (ts.isCallExpression(node)) {
      for (const argument of node.arguments) {
        if (!ts.isObjectLiteralExpression(argument)) continue;
        const method = argument.properties.find((property): property is ts.PropertyAssignment =>
          ts.isPropertyAssignment(property) && property.name.getText(source) === "method");
        if (!method || !methods.has(method.initializer.getText(source).replace(/["']/g, ""))) continue;
        const methodName = method.initializer.getText(source).replace(/["']/g, "");
        found.push({
          line: source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1,
          route: node.arguments[0]?.getText(source) ?? "",
          method: methodName,
          canonical: hasCanonicalAncestor(node),
        });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

describe("frontend backend mutation coverage", () => {
  it("classifies every literal mutation call in core and packaged provider-config-sync", () => {
    const frontendRoot = join(process.cwd(), "src");
    const providerRoot = join(process.cwd(), "../provider-config-sync/packages/provider-config-sync-ui/src");
    const uncovered: string[] = [];
    for (const file of [...sourceFiles(frontendRoot), ...sourceFiles(providerRoot)]) {
      const key = file.startsWith(frontendRoot)
        ? `src/${relative(frontendRoot, file)}`
        : `../provider-config-sync/packages/provider-config-sync-ui/src/${relative(providerRoot, file)}`;
      for (const mutation of mutations(file)) {
        const excluded = frontendBackendMutationExclusions.some((rule) =>
          rule.file === key && rule.method === mutation.method && mutation.route.includes(rule.routeIncludes));
        if (!excluded && !mutation.canonical) {
          uncovered.push(`${key}:${mutation.line} ${mutation.method} ${mutation.route}`);
        }
      }
    }
    expect(uncovered).toEqual([]);
  });
});
