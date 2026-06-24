import { useMemo } from "react";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";

const LAST_DISMISSED_MILESTONE_KEY = "bc_donation_prompt_last_milestone";
const FIRST_DONATION_MILESTONE = 25;
const DONATION_MILESTONE_INTERVAL = 50;

export function donationMilestoneForSessionCount(sessionCount: number) {
  if (sessionCount < FIRST_DONATION_MILESTONE) return null;
  const completedIntervals = Math.floor(
    (sessionCount - FIRST_DONATION_MILESTONE) / DONATION_MILESTONE_INTERVAL,
  );
  return FIRST_DONATION_MILESTONE + completedIntervals * DONATION_MILESTONE_INTERVAL;
}

export const donationWelcomeStorage = {
  nextMilestone(sessionCount: number) {
    const milestone = donationMilestoneForSessionCount(sessionCount);
    if (milestone === null) return null;
    const dismissed = Number(localStorage.getItem(LAST_DISMISSED_MILESTONE_KEY) ?? "0");
    return dismissed >= milestone ? null : milestone;
  },
  dismissMilestone(milestone: number) {
    localStorage.setItem(LAST_DISMISSED_MILESTONE_KEY, String(milestone));
  },
};

interface DonationOption {
  id: string;
  title: string;
  amount: string;
  description: string;
}

const DONATION_OPTIONS: DonationOption[] = [
  {
    id: "stick",
    title: "Buy me a stick",
    amount: "$1",
    description: "A tiny nod that still counts.",
  },
  {
    id: "coffee",
    title: "Buy me a coffee",
    amount: "$5",
    description: "For the classic support button.",
  },
  {
    id: "hamburger",
    title: "Buy me a hamburger",
    amount: "$12",
    description: "For a proper meal break.",
  },
];

function validDonationUrl(value: string) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : "";
  } catch {
    return "";
  }
}

function donationUrl() {
  const explicitUrl = import.meta.env?.VITE_DONATION_URL?.trim();
  if (explicitUrl) return validDonationUrl(explicitUrl);
  const handle = import.meta.env?.VITE_DONATION_HANDLE?.trim();
  if (!handle) return "";
  return validDonationUrl(`https://www.buymeacoffee.com/${encodeURIComponent(handle)}`);
}

interface Props {
  open: boolean;
  milestone?: number | null;
  onClose: () => void;
}

export function DonationWelcomeModal({ open, milestone, onClose }: Props) {
  const configuredDonationUrl = useMemo(() => donationUrl(), []);
  useBackButtonDismiss(open && !!configuredDonationUrl, onClose);

  if (!open || !configuredDonationUrl) return null;

  const donate = () => {
    window.open(configuredDonationUrl, "_blank", "noopener,noreferrer");
  };

  return (
    <div className="modal-overlay donation-welcome-overlay" onClick={onClose}>
      <section
        className="modal-content donation-welcome"
        role="dialog"
        aria-modal="true"
        aria-labelledby="donation-welcome-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header donation-welcome-header">
          <div>
            <p className="donation-welcome-kicker">Welcome to Better Agent</p>
            <h2 id="donation-welcome-title">Support the work</h2>
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            &times;
          </button>
        </div>
        <div className="modal-body donation-welcome-body">
          <p className="donation-welcome-copy">
            {milestone
              ? `You have created ${milestone} sessions. If Better Agent saves you time, you can send a small donation.`
              : "If Better Agent saves you time, you can send a small donation."}
          </p>
          <div className="donation-option-grid">
            {DONATION_OPTIONS.map((option) => (
              <button
                key={option.id}
                type="button"
                className="donation-option"
                onClick={donate}
              >
                <span className="donation-option-main">
                  <span className="donation-option-title">{option.title}</span>
                  <span className="donation-option-description">
                    {option.description}
                  </span>
                </span>
                <span className="donation-option-amount">{option.amount}</span>
              </button>
            ))}
          </div>
        </div>
        <div className="modal-footer donation-welcome-footer">
          <button type="button" className="btn-secondary" onClick={onClose}>
            Not now
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={donate}
          >
            Buy me a coffee
          </button>
        </div>
      </section>
    </div>
  );
}

interface DonationRedirectNoticeProps {
  open: boolean;
  status: "success" | "return";
  checkoutId: string | null;
  onClose: () => void;
}

export function DonationRedirectNotice({
  open,
  status,
  checkoutId,
  onClose,
}: DonationRedirectNoticeProps) {
  useBackButtonDismiss(open, onClose);
  if (!open) return null;

  const success = status === "success";

  return (
    <div className="modal-overlay donation-welcome-overlay" onClick={onClose}>
      <section
        className="modal-content donation-redirect"
        role="dialog"
        aria-modal="true"
        aria-labelledby="donation-redirect-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2 id="donation-redirect-title">
            {success ? "Support received" : "Checkout closed"}
          </h2>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            &times;
          </button>
        </div>
        <div className="modal-body donation-welcome-body">
          <p className="donation-welcome-copy">
            {success
              ? "Thanks for supporting Better Agent."
              : "You returned from checkout without completing a payment."}
          </p>
          {success && checkoutId && (
            <div className="donation-checkout-id">
              <span>Checkout ID</span>
              <code>{checkoutId}</code>
            </div>
          )}
        </div>
        <div className="modal-footer donation-welcome-footer">
          <button type="button" className="btn-primary" onClick={onClose}>
            Continue
          </button>
        </div>
      </section>
    </div>
  );
}
