/**
 * Landing-page templates shown at /landing. Each one is a standalone page at
 * /landing/<slug>/ rendered without the store chrome (see routes/layout.tsx),
 * styled only with the --color-t-* theme tokens so every theme skin applies.
 */
export interface LandingTemplate {
  slug: string;
  name: string;
  /** The fictional company the page is written for. */
  brand: string;
  blurb: string;
  sections: string[];
  /** A theme id from lib/themes.ts that suits the page. */
  suggestedTheme: string;
}

export const LANDING_TEMPLATES: LandingTemplate[] = [
  {
    slug: "saas",
    name: "SaaS Product",
    brand: "Northwind",
    blurb:
      "B2B software launch: product mockup hero, feature grid, pricing with a billing toggle, FAQ.",
    sections: ["Hero", "Logos", "Features", "Pricing", "FAQ", "CTA"],
    suggestedTheme: "issue-tracker",
  },
  {
    slug: "app",
    name: "Mobile App Launch",
    brand: "Tidepool",
    blurb:
      "Consumer app: phone mockup, store badges, how-it-works steps, ratings and reviews.",
    sections: ["Hero", "Steps", "Feature tour", "Reviews", "Download"],
    suggestedTheme: "consumer-electronics",
  },
  {
    slug: "agency",
    name: "Creative Agency",
    brand: "Fieldwork Studio",
    blurb:
      "Studio portfolio: oversized type, case-study grid, services, process, contact form.",
    sections: ["Hero", "Work", "Services", "Process", "Contact"],
    suggestedTheme: "monochrome-editorial",
  },
  {
    slug: "event",
    name: "Conference / Event",
    brand: "Signal Summit",
    blurb:
      "Tech conference: live countdown, speaker grid, tabbed schedule, venue, ticket tiers.",
    sections: ["Hero", "Speakers", "Schedule", "Venue", "Tickets"],
    suggestedTheme: "mecha-command",
  },
  {
    slug: "local-service",
    name: "Local Service Business",
    brand: "Brightline Solar",
    blurb:
      "Home-services company: quote form above the fold, trust badges, savings estimator, reviews.",
    sections: ["Hero + quote", "Trust", "Services", "Estimator", "Reviews"],
    suggestedTheme: "solar-industrial",
  },
  {
    slug: "waitlist",
    name: "Startup Waitlist",
    brand: "Halcyon",
    blurb:
      "Pre-launch single screen: email capture, waitlist position, three reasons to care.",
    sections: ["Hero + signup", "Highlights"],
    suggestedTheme: "frontier-ai-lab",
  },
];
