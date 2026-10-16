// The seven dashboards from the information architecture, in navigation order.
export interface DashboardInfo {
  path: string;
  title: string;
  question: string;
  // Roadmap phase that delivers this dashboard's data; null when it works today.
  dataPhase: number | null;
}

export const DASHBOARDS: readonly DashboardInfo[] = [
  { path: "/", title: "Zentrale", question: "Was braucht mich heute?", dataPhase: null },
  { path: "/agents", title: "Agenten", question: "Ist der Schwarm gesund, und was kostet er?", dataPhase: null },
  { path: "/trading", title: "Trading", question: "Wie laufen die Bots, und halten sie die Risikolimits ein?", dataPhase: 6 },
  { path: "/evolution", title: "Selbst-Evolution", question: "Was hat das System gelernt, und was will es ändern?", dataPhase: 7 },
  { path: "/content", title: "Content", question: "Was ist in der Pipeline, und was ist bereit zur Veröffentlichung?", dataPhase: null },
  { path: "/intel", title: "Social Intelligence", question: "Was habe ich gespeichert, und was wurde daraus?", dataPhase: 4 },
  { path: "/finance", title: "Finanzen", question: "Trägt sich das System selbst?", dataPhase: 4 },
];
