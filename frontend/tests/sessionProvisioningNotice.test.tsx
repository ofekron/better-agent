import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SessionProvisioningNotice } from "../src/components/SessionProvisioningNotice";

describe("SessionProvisioningNotice", () => {
  it("renders nothing when the session has nothing pending", () => {
    const { container } = render(
      <SessionProvisioningNotice provisioning={undefined} />,
    );
    expect(container.querySelector(".session-provisioning")).toBeNull();
  });

  it("clears once the backend reports provisioning finished (explicit null)", () => {
    const { container } = render(<SessionProvisioningNotice provisioning={null} />);
    expect(container.querySelector(".session-provisioning")).toBeNull();
  });

  it("shows an indeterminate preparing state while pending", () => {
    const { container, getByText } = render(
      <SessionProvisioningNotice provisioning={{ state: "pending" }} />,
    );
    const notice = container.querySelector(".session-provisioning");
    expect(notice).toBeTruthy();
    expect(notice?.classList.contains("is-failed")).toBe(false);
    // Announced to assistive tech without stealing focus from the composer.
    expect(notice?.getAttribute("role")).toBe("status");
    expect(getByText("Preparing this session…")).toBeTruthy();
    // Tells the user their prompt is queued rather than lost.
    expect(
      getByText(
        "Anything you send now is queued and delivered as soon as it is ready.",
      ),
    ).toBeTruthy();
  });

  it("shows a truthful failure with the backend reason", () => {
    const { container, getByText } = render(
      <SessionProvisioningNotice
        provisioning={{ state: "failed", error: "RuntimeError: no provider" }}
      />,
    );
    expect(
      container.querySelector(".session-provisioning")?.classList.contains("is-failed"),
    ).toBe(true);
    expect(getByText("Session setup failed")).toBeTruthy();
    expect(
      getByText("RuntimeError: no provider — send a message to try again."),
    ).toBeTruthy();
  });

  it("still offers a way forward when the failure carries no reason", () => {
    const { getByText } = render(
      <SessionProvisioningNotice provisioning={{ state: "failed" }} />,
    );
    expect(getByText("Send a message to try again.")).toBeTruthy();
  });
});
