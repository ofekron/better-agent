import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import {
  DonationRedirectNotice,
  DonationWelcomeModal,
  donationMilestoneForSessionCount,
  donationWelcomeStorage,
} from "../src/components/DonationWelcomeModal";

describe("DonationWelcomeModal", () => {
  beforeEach(() => {
    vi.unstubAllEnvs();
    vi.stubGlobal("open", vi.fn());
  });

  it("computes donation milestones from session counts", () => {
    expect(donationMilestoneForSessionCount(24)).toBeNull();
    expect(donationMilestoneForSessionCount(25)).toBe(25);
    expect(donationMilestoneForSessionCount(74)).toBe(25);
    expect(donationMilestoneForSessionCount(75)).toBe(75);
    expect(donationMilestoneForSessionCount(125)).toBe(125);
  });

  it("persists dismissal per donation milestone", () => {
    expect(donationWelcomeStorage.nextMilestone(25)).toBe(25);

    donationWelcomeStorage.dismissMilestone(25);

    expect(donationWelcomeStorage.nextMilestone(74)).toBeNull();
    expect(donationWelcomeStorage.nextMilestone(75)).toBe(75);
  });

  it("does not show donation actions without a valid donation destination", () => {
    const { unmount } = render(
      <DonationWelcomeModal open milestone={25} onClose={() => {}} />,
    );

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText(/created 25 sessions/i)).toBeNull();
    expect(screen.queryByText(/buy me a coffee/i)).toBeNull();

    unmount();
    vi.stubEnv("VITE_DONATION_URL", "not a url");
    render(<DonationWelcomeModal open milestone={25} onClose={() => {}} />);

    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens the configured Buy Me a Coffee handle", () => {
    vi.stubEnv("VITE_DONATION_HANDLE", "ofek");
    render(<DonationWelcomeModal open onClose={() => {}} />);

    const buyCoffeeButtons = screen.getAllByRole("button", {
      name: /buy me a coffee/i,
    });
    fireEvent.click(buyCoffeeButtons[buyCoffeeButtons.length - 1]);

    expect(window.open).toHaveBeenCalledWith(
      "https://www.buymeacoffee.com/ofek",
      "_blank",
      "noopener,noreferrer",
    );
  });

  it("shows Polar success details with checkout id", () => {
    render(
      <DonationRedirectNotice
        open
        status="success"
        checkoutId="chk_123"
        onClose={() => {}}
      />,
    );

    expect(screen.queryByText("Support received")).not.toBeNull();
    expect(screen.queryByText("chk_123")).not.toBeNull();
  });

  it("shows Polar return without checkout id", () => {
    render(
      <DonationRedirectNotice
        open
        status="return"
        checkoutId={null}
        onClose={() => {}}
      />,
    );

    expect(screen.queryByText("Checkout closed")).not.toBeNull();
    expect(screen.queryByText(/without completing a payment/i)).not.toBeNull();
  });
});
