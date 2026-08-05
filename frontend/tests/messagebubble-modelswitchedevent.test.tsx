import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { ModelSwitchedEvent } from "../src/components/MessageBubble";

// ModelSwitchedEvent renders the "model switched / reasoning changed /
// runtime changed" notice from a `model_switched` event payload. The test
// i18n instance (tests/setup.ts) has empty resources, so t(key) returns the
// key verbatim and t(key, { defaultValue }) returns the default — those
// stable strings are what we assert on. Branch coverage targets: the null
// early-return, the label ternary (runtimeChanged / modelSwitched /
// reasoningChanged), the render ternary (from→to vs to-only vs reasoning),
// the reasoning merge ternary, includeEffort/includeRunner gates, the
// provider-kind truthy/falsy coalesce paths in fromProvider/toProvider, and
// providerDisplayName's name+nickname formatting.

const renderSwitch = (data: Record<string, unknown>) =>
  render(<ModelSwitchedEvent data={data} />).container;

const text = (data: Record<string, unknown>) =>
  renderSwitch(data).querySelector(".event-model-switched")?.textContent ?? "";

describe("ModelSwitchedEvent early return", () => {
  it("renders nothing when model/provider_id/reasoning_effort/runner are all absent", () => {
    // Only previous_* fields -> the guard short-circuits to null.
    const c = renderSwitch({ previous_model: "old", previous_provider_id: "x" });
    expect(c.firstChild).toBeNull();
  });
});

describe("ModelSwitchedEvent model change", () => {
  it("renders '<from> to <to>' with the modelSwitched label", () => {
    // changed includes "model" -> hasModelChange. previous_provider_id with no
    // previous_provider_kind exercises the `|| previousProviderId` fallback.
    const c = renderSwitch({
      model: "gpt5",
      previous_model: "gpt4",
      previous_provider_id: "anthropic",
      changed: ["model"],
    });
    const body = c.querySelector(".event-model-switched")!.textContent!;
    expect(body).toContain("message.modelSwitched");
    expect(body).toContain("anthropic / gpt4 to");
    expect(body).toContain("gpt5");
  });

  it("renders to-only when there is no previous state", () => {
    // from is empty -> the `from && to ? ... : to` else branch.
    const body = text({ model: "gpt5", provider_id: "openai", changed: ["provider_id"] });
    expect(body).toContain("message.modelSwitched");
    expect(body).toContain("openai");
    expect(body).toContain("gpt5");
    expect(body).not.toContain(" to ");
  });
});

describe("ModelSwitchedEvent reasoning change", () => {
  it("renders the merged '<prev> to <cur>' reasoning with the reasoningChanged label", () => {
    // hasReasoningChange only -> label reasoningChanged, render second span,
    // reasoning merge truthy branch.
    const body = text({
      model: "gpt5",
      reasoning_effort: "high",
      previous_reasoning_effort: "low",
      changed: ["reasoning_effort"],
    });
    expect(body).toContain("message.reasoningChanged");
    expect(body).toContain("low to high");
  });

  it("renders the current effort alone when no previous effort exists", () => {
    // reasoning merge false branch (previousReasoningEffort falsy).
    const body = text({
      model: "gpt5",
      reasoning_effort: "high",
      changed: ["reasoning_effort"],
    });
    expect(body).toContain("message.reasoningChanged");
    expect(body).toContain("high");
    expect(body).not.toContain("to high");
  });
});

describe("ModelSwitchedEvent runner change", () => {
  it("renders the runtimeChanged label and includes runner labels", () => {
    // hasRunnerChange -> label runtimeChanged, includeRunner true, provider
    // kinds present -> runtimeKindLabelKey coalesce path for from/toProvider.
    const body = text({
      model: "gpt5",
      provider_kind: "claude",
      previous_provider_kind: "claude",
      previous_provider_name: "Anthropic",
      previous_provider_nickname: "team",
      runner: "better_agent_runner",
      previous_runner: "native",
      changed: ["runner"],
    });
    expect(body).toContain("message.runtimeChanged");
    // Runner defaultValues flow through t(key, defaultValue) under empty i18n.
    expect(body).toContain("native");
    expect(body).toContain("better_agent_runner");
    expect(body).toContain("gpt5");
    // previous_provider_name + nickname flow through providerDisplayName.
    expect(body).toContain("Anthropic · team");
  });
});

describe("ModelSwitchedEvent provider display name", () => {
  it("formats provider name + nickname and falls back to provider_id", () => {
    // provider_name + provider_nickname exercise providerDisplayName's
    // `${base} · ${nickname}` branch; changed absent exercises the
    // Array.isArray false branch; provider_id is the display fallback base.
    const body = text({
      model: "gpt5",
      provider_id: "openai",
      provider_name: "OpenAI",
      provider_nickname: "work",
    });
    expect(body).toContain("OpenAI · work");
    expect(body).toContain("gpt5");
  });
});
