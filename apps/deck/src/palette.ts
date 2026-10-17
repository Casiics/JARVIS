// Command palette (⌘K): the deck's search and command field from the information architecture.
// Pure helpers, so the matching and ordering can be tested without a browser.

export interface Command {
  id: string;
  title: string;
  // What choosing the command does, shown next to the title ("every card states its action").
  hint: string;
  group: "Navigation" | "Aktion";
  keywords: readonly string[];
}

const fold = (s: string): string =>
  s
    .toLocaleLowerCase("de-DE")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "");

/** Commands whose title or keywords contain every word of the query; title matches first. */
export function filterCommands(commands: readonly Command[], query: string): Command[] {
  const words = fold(query).split(/\s+/).filter(Boolean);
  if (words.length === 0) return [...commands];
  const scored: { command: Command; score: number }[] = [];
  for (const command of commands) {
    const title = fold(command.title);
    const haystack = [title, ...command.keywords.map(fold)].join(" ");
    if (!words.every((w) => haystack.includes(w))) continue;
    const score = words.reduce(
      (acc, w) => acc + (title.startsWith(w) ? 3 : title.includes(w) ? 2 : 1),
      0,
    );
    scored.push({ command, score });
  }
  return scored.sort((a, b) => b.score - a.score).map((s) => s.command);
}

/** Arrow keys wrap around the list; an empty list keeps the selection at 0. */
export function moveSelection(current: number, delta: number, length: number): number {
  if (length === 0) return 0;
  return (((current + delta) % length) + length) % length;
}

export function isPaletteShortcut(e: Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey">): boolean {
  return (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k";
}
