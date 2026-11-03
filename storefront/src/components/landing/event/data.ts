/** Content for the Signal Summit (event) landing template. All fictional. */

/** Doors open on day one, Rotterdam local time (CEST, UTC+2). */
export const EVENT_START = Date.parse("2027-04-14T09:00:00+02:00");

export interface Speaker {
  name: string;
  initials: string;
  role: string;
  company: string;
  talk: string;
  track: Track;
}

export type Track = "Infra" | "Platform" | "Security" | "Data";

export const SPEAKERS: Speaker[] = [
  {
    name: "Ines Varga-Holt",
    initials: "IV",
    role: "Principal SRE",
    company: "Kettlewire",
    talk: "Our pager went off 2,300 times in a quarter. Here is what we deleted.",
    track: "Infra",
  },
  {
    name: "Tomasz Okafor",
    initials: "TO",
    role: "Staff Engineer, Networking",
    company: "Quayline Freight",
    talk: "BGP for application developers: one outage, slowed down",
    track: "Infra",
  },
  {
    name: "Mireille Santos",
    initials: "MS",
    role: "Head of Platform",
    company: "Brightfold Bank",
    talk: "Paved roads that people actually drive on",
    track: "Platform",
  },
  {
    name: "Arjun Dekker",
    initials: "AD",
    role: "Security Engineer",
    company: "Northgate Health",
    talk: "Rotating 41,000 secrets without a maintenance window",
    track: "Security",
  },
  {
    name: "Saoirse Lindqvist",
    initials: "SL",
    role: "Database Reliability Lead",
    company: "Parcelpoint",
    talk: "Postgres at 90% disk: a play in five migrations",
    track: "Data",
  },
  {
    name: "Kwame Albrecht",
    initials: "KA",
    role: "Compiler Engineer",
    company: "Ferrous Labs",
    talk: "Shaving 38% off cold starts by reading the linker map",
    track: "Platform",
  },
  {
    name: "Yuki Brennan",
    initials: "YB",
    role: "Observability Architect",
    company: "Tidewater Games",
    talk: "Sampling traces like you mean it: 1 in 1,000, and nothing lost",
    track: "Data",
  },
  {
    name: "Leonie Achterberg",
    initials: "LA",
    role: "CTO",
    company: "Rookhaven",
    talk: "Closing keynote: the boring stack wins again",
    track: "Security",
  },
];

export interface Session {
  time: string;
  title: string;
  who: string;
  track: Track | "Plenary";
  room: string;
}

export const SCHEDULE: { label: string; date: string; sessions: Session[] }[] =
  [
    {
      label: "Day 1",
      date: "Wed 14 April",
      sessions: [
        {
          time: "08:00",
          title: "Badge pickup, coffee, and stroopwafels",
          who: "Ground floor",
          track: "Plenary",
          room: "Foyer",
        },
        {
          time: "09:00",
          title: "Opening keynote: the outage budget",
          who: "Ines Varga-Holt",
          track: "Infra",
          room: "Main Hall",
        },
        {
          time: "10:15",
          title: "Paved roads that people actually drive on",
          who: "Mireille Santos",
          track: "Platform",
          room: "Stage 2",
        },
        {
          time: "11:30",
          title: "BGP for application developers",
          who: "Tomasz Okafor",
          track: "Infra",
          room: "Main Hall",
        },
        {
          time: "13:30",
          title: "Rotating 41,000 secrets without a window",
          who: "Arjun Dekker",
          track: "Security",
          room: "Stage 3",
        },
        {
          time: "15:00",
          title: "Workshop: build a rate limiter that survives a stampede",
          who: "Hallway crew",
          track: "Platform",
          room: "Lab A",
        },
        {
          time: "17:30",
          title: "Harbor social on the Maashaven terrace",
          who: "Everyone",
          track: "Plenary",
          room: "Terrace",
        },
      ],
    },
    {
      label: "Day 2",
      date: "Thu 15 April",
      sessions: [
        {
          time: "08:30",
          title: "Breakfast lightning talks: 5 minutes, no slides over 12",
          who: "Open mic",
          track: "Plenary",
          room: "Main Hall",
        },
        {
          time: "09:30",
          title: "Postgres at 90% disk: a play in five migrations",
          who: "Saoirse Lindqvist",
          track: "Data",
          room: "Main Hall",
        },
        {
          time: "10:45",
          title: "Shaving 38% off cold starts with the linker map",
          who: "Kwame Albrecht",
          track: "Platform",
          room: "Stage 2",
        },
        {
          time: "12:00",
          title: "Sampling traces like you mean it",
          who: "Yuki Brennan",
          track: "Data",
          room: "Stage 3",
        },
        {
          time: "14:00",
          title: "Red team debrief: one badge, four floors",
          who: "Northgate Health security",
          track: "Security",
          room: "Stage 2",
        },
        {
          time: "15:30",
          title: "Workshop: chaos drills for a 3-person on-call",
          who: "Hallway crew",
          track: "Infra",
          room: "Lab A",
        },
        {
          time: "16:45",
          title: "Closing keynote: the boring stack wins again",
          who: "Leonie Achterberg",
          track: "Security",
          room: "Main Hall",
        },
      ],
    },
  ];

/** Token-only dot colour for each track tag (the label itself stays text-t-text for contrast). */
export const TRACK_DOT: Record<Session["track"], string> = {
  Infra: "bg-t-accent",
  Platform: "bg-t-info",
  Security: "bg-t-error",
  Data: "bg-t-success",
  Plenary: "bg-t-muted",
};
