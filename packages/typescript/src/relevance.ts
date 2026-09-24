/** Local matching over original Unicode scalar offsets; mirrors relevance.py. */
const STOP_WORDS = new Set((
  "a an and are as at be been by can could did do does for from had has have how i in is it " +
  "me of on or our please should that the their this to us was we were what when where which " +
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

const KEY_MEMO = new Map<string, string>();

/** Generic suffix rules only; spec/fixtures/term-normalization.json pins them. Memoized like relevance.py. */
function key(word: string): string {
  const cached = KEY_MEMO.get(word);
  if (cached !== undefined) return cached;
  if (KEY_MEMO.size >= 65_536) KEY_MEMO.clear();
  const result = computeKey(word);
  KEY_MEMO.set(word, result);
  return result;
}

function computeKey(word: string): string {
  if (word.length < 4 || !/^[a-z]+$/.test(word)) return word;
  if (word.length === 4) {
    if (word.endsWith("e") || (word.endsWith("s") && !["ss", "us", "is"].some(suffix => word.endsWith(suffix)))) return word.slice(0, -1);
    // ponytail: "used" -> "use" also maps "shed" -> "she"; FTS5 porter stemming if collisions matter.
    return word.endsWith("ed") && !"aeiou".includes(word[1]) ? word.slice(0, -1) : word;
  }
  let base: string;
  let doubled = false;
  if (word.endsWith("ies")) return word.slice(0, -3) + "y";
  if (word.endsWith("ied")) return word.slice(0, -3) + "y";
  if (word.endsWith("ing") && word.length >= 6) { base = word.slice(0, -3); doubled = true; }
  else if (word.endsWith("ed") && word.length >= 5) { base = word.slice(0, -2); doubled = true; }
  else if (word.endsWith("es") && word.length >= 5) base = word.slice(0, -2);
  else if (word.endsWith("s") && !["ss", "us", "is"].some(suffix => word.endsWith(suffix))) base = word.slice(0, -1);
  else if (word.endsWith("e")) base = word.slice(0, -1);
  else return word;
  if (doubled && base.length > 2 && base.at(-1) === base.at(-2) && !"aeiou".includes(base.at(-1)!)) base = base.slice(0, -1);
  return base;
}

export function queryTerms(query: string): string[] {
  return [...new Set([...words(Array.from(query).slice(0, 4096).join(""))]
    .map(([, , word]) => word).filter(word => word && !STOP_WORDS.has(word)).map(key))].slice(0, 32);
}

export function ftsTermExpression(term: string): string {
  if (term.length < 3 || !/^[a-z]+$/.test(term)) return `"${term.replace(/"/g, '""')}"`;
  const forms = [term, term + "e", term + "s", term + "es", term + "ed", term + "ing"];
  if (term && !"aeiouy".includes(term.at(-1)!)) forms.push(term + term.at(-1)! + "ed", term + term.at(-1)! + "ing");
  if (term.endsWith("e")) forms.push(term + "d");
  if (term.endsWith("y")) forms.push(term.slice(0, -1) + "ies", term.slice(0, -1) + "ied");
  return "(" + [...new Set(forms)].filter(form => key(form) === term)
    .map(form => `"${form.replace(/"/g, '""')}"`).join(" OR ") + ")";
}

export function relevance(text: string, terms: string[]): number {
  if (!terms.length) return 0;
  const wanted = new Set(terms), found = new Set<string>();
  for (const [, , word] of words(text)) { const stem = key(word); if (wanted.has(stem)) found.add(stem); }
  return found.size;
}

export function excerptForQuery(text: string, terms: string[], window: number): string {
  const chars = Array.from(text);
  if (chars.length <= window || !terms.length) return chars.slice(0, window).join("");
  const wanted = new Set(terms), matches = [...words(text)].map(([start, end, word]) => [start, end, key(word)] as [number, number, string])
    .filter(([, , word]) => wanted.has(word));
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
