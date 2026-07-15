import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";
import { sessionLinkMarker } from "../src/utils/linkifyFilePaths";
import { MessageBubble } from "../src/components/MessageBubble";

describe("user session marker rendering", () => {
  it("renders copied session markers as portable links in paired user turns", async () => {
    const marker = sessionLinkMarker("linked-session", "Linked Session");
    const session = makeSession({
      id: "marker-host",
      messages: [
        makeUserMsg({ id: "u1", content: `${marker} check this session` }),
        makeAssistantMsg({ id: "a1", content: "done" }),
      ],
    });

    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("marker-host");

    const userGroup = h.$("#msg-u1")?.closest(".turn-group");
    const renderedMarkdown = userGroup?.querySelector<HTMLElement>("[data-test-md='true']");
    expect(userGroup?.textContent).not.toContain("[[ba-session:");
    expect(renderedMarkdown?.textContent).toContain("[Linked Session · link](/s/linked-session)");

    h.unmount();
  });

  it("renders copied session markers inside unwrapped artificial user sections", async () => {
    const marker = sessionLinkMarker("section-session", "Section Session");
    const session = makeSession({
      id: "marker-section-host",
      messages: [
        makeUserMsg({ id: "u1", content: `<user_prompt>${marker} from section</user_prompt>` }),
        makeAssistantMsg({ id: "a1", content: "done" }),
      ],
    });

    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("marker-section-host");

    const userGroup = h.$("#msg-u1")?.closest(".turn-group");
    const renderedMarkdown = userGroup?.querySelector<HTMLElement>("[data-test-md='true']");
    expect(userGroup?.textContent).not.toContain("[[ba-session:");
    expect(renderedMarkdown?.textContent).toContain("[Section Session · sect](/s/section-session)");

    h.unmount();
  });

  it("renders copied session markers as portable links in standalone user bubbles", () => {
    const marker = sessionLinkMarker("standalone-session", "Standalone Session");
    const { container, unmount } = render(
      <MessageBubble
        message={makeUserMsg({ id: "standalone-user", content: `${marker} standalone` })}
        orchestrationMode="native"
      />,
    );

    const renderedMarkdown = container.querySelector<HTMLElement>("[data-test-md='true']");
    expect(container.textContent).not.toContain("[[ba-session:");
    expect(renderedMarkdown?.textContent).toContain("[Standalone Session · stan](/s/standalone-session)");

    unmount();
  });
});
