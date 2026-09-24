/** Local matching over original Unicode scalar offsets; mirrors relevance.py. */
const STOP_WORDS = new Set((
  "a an and are as at be been by can could did do does for from had has have how i in is it " +
  "me of on or our please should that the their this to us was were what when where which " +
  "who why will with would you your"
).split(" "));

function* words(text: string): Generator<[number, number, string]> {
  const chars = Array.from(text);
  let start: number | null = null;
  for (let end = 0; end <= chars.length; end++) {
    if (end < chars.length && /[\p{L}\p{N}\p{M}]/u.test(chars[end])) {
      if (start === null) start = end;
    } else if (start !== null) {
      const word = chars.slice(start, end).join("").normalize("NFD").toLowerCase().replace(/\p{M}/gu, "");
      yield [start, end, word];
      start = null;
    }
  }
}

export function queryTerms(query: string): string[] {
  return [...new Set([...words(Array.from(query).slice(0, 4096).join(""))]
    .map(([, , word]) => word).filter(word => word && !STOP_WORDS.has(word)))].slice(0, 32);
}

export function relevance(text: string, terms: string[]): number {
  if (!terms.length) return 0;
  const wanted = new Set(terms), found = new Set<string>();
  for (const [, , word] of words(text)) if (wanted.has(word)) found.add(word);
  return found.size;
}

export function excerptForQuery(text: string, terms: string[], window: number): string {
  const chars = Array.from(text);
  if (chars.length <= window || !terms.length) return chars.slice(0, window).join("");
  const wanted = new Set(terms), matches = [...words(text)].filter(([, , word]) => wanted.has(word));
  const counts = new Map<string, number>();
  let left = 0, bestScore = 0, bestStart = 0, bestEnd = 0;
  for (let right = 0; right < matches.length; right++) {
    const [, end, word] = matches[right];
    counts.set(word, (counts.get(word) ?? 0) + 1);
    while (left <= right && end - matches[left][0] > window) {
      const removed = matches[left][2], count = counts.get(removed)! - 1;
      if (count) counts.set(removed, count); else counts.delete(removed);
      left++;
    }
    if (counts.size && counts.size >= bestScore) {
      bestScore = counts.size; bestStart = matches[left][0]; bestEnd = end;
    }
  }
  const start = Math.min(Math.max(0, bestEnd - window, bestStart - Math.floor(window / 4)), Math.max(0, chars.length - window));
  return chars.slice(start, start + window).join("");
}
