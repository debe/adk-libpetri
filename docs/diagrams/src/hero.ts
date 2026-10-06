/**
 * The README's opening figure, svg/hero.svg, composed from generated files
 * only, so nothing in it is drawn by hand:
 *
 * - hero/race.yaml and hero/race_naive.yaml: copies of the blueprints in
 *   python/tests/readme_diagrams/hero/, golden-checked by that test. The
 *   naive twin guards its commit with `inhibit: [won]` instead of a permit;
 * - svg/hero-race.svg and svg/hero-race-naive.svg: graphviz's renders of the
 *   nets the two files build, exported by the same test;
 * - hero/verify-*.txt: `adk-libpetri verify` output for both files, verbatim,
 *   cut to verdicts, firings and the telling markings of each counterexample.
 *
 * Three columns: the fixed blueprint in full, then the naive net and what Z3
 * finds in it, then the fixed net and its proofs. The element that differs is
 * highlighted in each net: the inhibitor arc in red, the permit in green.
 *
 * Run after `npm run render` (`npm run build` does both).
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const ROOT = join(import.meta.dirname, '..');
const read = (p: string) => readFileSync(join(ROOT, p), 'utf-8');

const W = 1200;
const PAD = 16;
const GAP = 12;
const YAML_W = 470;
const COL_W = (W - 2 * PAD - YAML_W - 2 * GAP) / 2;
const LINE = 15.5;
const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
const SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif";
const RED = '#cf222e';
const GREEN = '#1a7f37';
const BLUE = '#0550ae';
const INK = '#1f2328';
const MUTED = '#59636e';

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

type Span = { text: string; fill?: string; weight?: number };

function textLine(x: number, y: number, spans: Span[], size = 11.5): string {
  const t = spans
    .map((s) => `<tspan${s.fill ? ` fill="${s.fill}"` : ''}${s.weight ? ` font-weight="${s.weight}"` : ''}>${esc(s.text)}</tspan>`)
    .join('');
  return `<text x="${x}" y="${y}" font-family="${MONO}" font-size="${size}" fill="${INK}" xml:space="preserve">${t}</text>`;
}

function prose(x: number, y: number, html: string, size = 12.5, weight = 400): string {
  return `<text x="${x}" y="${y}" font-family="${SANS}" font-size="${size}" font-weight="${weight}" fill="${INK}">${html}</text>`;
}

const code = (t: string, fill = INK) => `<tspan font-family="${MONO}" fill="${fill}">${esc(t)}</tspan>`;

/** YAML with keys in blue, `mark` (a word) in `colour`, or the whole line in `colour`. */
function yamlSpans(line: string, mark: string | null, colour: string, whole = false): Span[] {
  if (whole) return [{ text: line, fill: colour, weight: 700 }];
  const m = line.match(/^(\s*)([A-Za-z_][\w]*)(:)(.*)$/);
  const [ws, key, colon, rest] = m ? m.slice(1) : ['', '', '', line];
  const spans: Span[] = key ? [{ text: ws }, { text: key, fill: key === mark ? colour : BLUE }, { text: colon }] : [];
  const parts = mark ? rest.split(new RegExp(`\\b(${mark})\\b`)) : [rest];
  for (const p of parts) spans.push(p === mark ? { text: p, fill: colour, weight: 700 } : { text: p });
  return spans;
}

function verifySpans(line: string): Span[] {
  const m = line.match(/^(PROVEN|VIOLATED|UNKNOWN)(\s+)(.*)$/);
  if (!m) return [{ text: line, fill: line.match(/^\d+ proven/) ? MUTED : undefined }];
  const fill = m[1] === 'PROVEN' ? GREEN : m[1] === 'VIOLATED' ? RED : '#9a6700';
  return [{ text: m[1], fill, weight: 700 }, { text: m[2] + m[3] }];
}

/** Wraps a verify line at " -> " (firings) or before " [" (the claim kind). */
function wrap(line: string, chars: number): string[] {
  if (line.length <= chars) return [line];
  const kind = line.match(/^(.*\S)(\s+\[\w+\])$/);
  if (kind) return [kind[1], ' '.repeat(9) + kind[2].trim()];
  const indent = ' '.repeat(line.search(/\S/) + 'fires: '.length);
  const out: string[] = [];
  let cur = '';
  for (const part of line.split(' -> ')) {
    const next = cur ? `${cur} -> ${part}` : part;
    if (next.length > chars && cur) {
      out.push(`${cur} ->`);
      cur = indent + part;
    } else cur = next;
  }
  out.push(cur);
  return out;
}

function panel(x: number, y: number, w: number, h: number, stroke = '#d0d7de', fill = '#f6f8fa'): string {
  return `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="6" fill="${fill}" stroke="${stroke}"/>`;
}

/** A column header: a coloured bar with a mark and a title. */
function header(x: number, y: number, w: number, colour: string, tint: string, title: string, note = ''): string {
  return [
    `<rect x="${x}" y="${y}" width="${w}" height="30" rx="6" fill="${tint}" stroke="${colour}"/>`,
    `<text x="${x + 12}" y="${y + 20}" font-family="${SANS}" font-size="14" font-weight="700" fill="${colour}">${esc(title)}`,
    note ? `<tspan font-size="12" font-weight="400" fill="${MUTED}">   ${esc(note)}</tspan>` : '',
    `</text>`,
  ].join('');
}

/**
 * The rendered net as a nested <svg> scaled to `width`, its ids prefixed (two
 * nets share one document) and the elements titled in `highlight` restyled.
 */
const viewBox = (svg: string) => svg.match(/viewBox="([\d.\s-]+)"/)![1].split(/\s+/).map(Number);

function nestedNet(
  svg: string,
  prefix: string,
  x: number,
  y: number,
  scale: number,
  highlight: Record<string, (g: string) => string>,
): { body: string; width: number; height: number } {
  let root = svg.slice(svg.indexOf('<svg'));
  const vb = viewBox(root);
  const width = vb[2] * scale;
  const height = vb[3] * scale;
  root = root.replace(/ id="([^"]+)"/g, ` id="${prefix}-$1"`);
  const seen = new Set<string>();
  root = root.replace(/<g id="[^"]+" class="(?:node|edge)">\n?<title>([^<]+)<\/title>[\s\S]*?<\/g>/g, (g, title: string) => {
    const name = title.replace(/&#45;/g, '-').replace(/&gt;/g, '>');
    const restyle = highlight[name];
    if (!restyle) return g;
    seen.add(name);
    return restyle(g);
  });
  const missing = Object.keys(highlight).filter((k) => !seen.has(k));
  if (missing.length) throw new Error(`${prefix}: nothing titled ${missing.join(', ')} to highlight`);
  const open = root.match(/<svg[^>]*>/)![0];
  const body = root.replace(
    open,
    `<svg x="${(x - width / 2).toFixed(1)}" y="${y}" width="${width.toFixed(1)}" height="${height.toFixed(1)}" viewBox="${vb.join(' ')}">`,
  );
  return { body, width, height };
}

const recolour = (colour: string, width: number) => (g: string) =>
  g
    .replace(/stroke="[^"]+"/g, `stroke="${colour}"`)
    .replace(/(<polygon[^>]*?)fill="#[0-9a-fA-F]+"/g, `$1fill="${colour}"`)
    .replace(/<(path|ellipse|polygon) /g, `<$1 stroke-width="${width}" `);
const fillPlace = (fill: string, stroke: string) => (g: string) =>
  g.replace(/<ellipse fill="[^"]+" stroke="[^"]+" stroke-width="[^"]+"/, `<ellipse fill="${fill}" stroke="${stroke}" stroke-width="3"`);

// ---- inputs -----------------------------------------------------------------

const good = read('hero/race.yaml').trimEnd().split('\n');
const naive = read('hero/race_naive.yaml').trimEnd().split('\n');
const words = (ls: string[]) => new Set(ls.join(' ').split(/[^\w]+/));
const naiveWords = words(naive);
const only = [...words(good)].filter((w) => w && !naiveWords.has(w) && !good.includes(`name: ${w}`));
if (only.length !== 1) throw new Error(`expected one word only race.yaml has, got ${only}`);
const permit = only[0];

/** A file's Race_Commit transition, block-style lines included. */
function commitOf(lines: string[]): string[] {
  const start = lines.findIndex((l) => /^  Race_Commit:/.test(l));
  let end = start + 1;
  while (end < lines.length && lines[end].startsWith('    ')) end++;
  return lines.slice(start, end);
}
const goodCommit = commitOf(good);
const naiveCommit = commitOf(naive);
const NETS = { naive: read('svg/hero-race-naive.svg'), fixed: read('svg/hero-race.svg') };

const CHARS = Math.floor((COL_W - 28) / 6.7);
const verifyGood = read('hero/verify-race.txt').trimEnd().split('\n').flatMap((l) => wrap(l, CHARS));
const verifyNaive = read('hero/verify-race_naive.txt').trimEnd().split('\n').flatMap((l) => wrap(l, CHARS));
const busy = verifyNaive.map((l) => l.match(/^\s+(\d+): \{inflight:Race_Commit: 2\}/)).find(Boolean);
if (!busy) throw new Error('the naive counterexample no longer shows two commits in flight');

// ---- layout -----------------------------------------------------------------

const out: string[] = [];
const top = PAD;
// One scale for both nets, so the same place is drawn the same size in each.
const SCALE = (COL_W - 50) / Math.max(viewBox(NETS.naive)[2], viewBox(NETS.fixed)[2]);

/** One comparison column: header, the commit as written, the net, the verdicts. */
function column(
  x: number,
  colour: string,
  tint: string,
  title: string,
  commit: string[],
  changed: (l: string) => boolean,
  netSvg: string,
  prefix: string,
  highlight: Record<string, (g: string) => string>,
  note: string[],
  verify: string[],
): number {
  let y = top;
  out.push(header(x, y, COL_W, colour, tint, title));
  y += 30 + 22;
  for (const l of commit) {
    out.push(textLine(x + 14, y, yamlSpans(l, null, colour, changed(l))));
    y += LINE;
  }
  y += 6;
  for (const n of note) {
    out.push(prose(x + 14, y, n, 12));
    y += LINE;
  }
  const net = nestedNet(netSvg, prefix, x + COL_W / 2, y, SCALE, highlight);
  out.push(net.body);
  y += net.height + 10;
  out.push(prose(x + 14, y, '$ adk-libpetri verify', 12, 600));
  y += LINE + 2;
  for (const l of verify) {
    out.push(textLine(x + 14, y, verifySpans(l), 11));
    y += LINE - 1;
  }
  return y;
}

const X1 = PAD + YAML_W + GAP;
const X2 = X1 + COL_W + GAP;
const bottomNaive = column(
  X1, RED, '#ffebe9', '✗  The obvious guard',
  naiveCommit, (l) => !goodCommit.includes(l),
  NETS.naive, 'naive',
  { 'p_won->t_Race_Commit': recolour(RED, 3.5) },
  [`${code('won', RED)} inhibits ${code('Race_Commit')}:`, 'commit only while nobody has won.'],
  verifyNaive,
);
const bottomGood = column(
  X2, GREEN, '#dafbe1', '✓  The fix: a permit',
  goodCommit, (l) => !naiveCommit.includes(l),
  NETS.fixed, 'fixed',
  {
    p_permit: fillPlace('#dafbe1', GREEN),
    'p_permit->t_Race_Commit': recolour(GREEN, 3),
    'j_Race_Start__and_0->p_permit': recolour(GREEN, 3),
  },
  [`${code('Race_Commit')} consumes the one ${code(permit, GREEN)}`, 'that Race_Start puts down.'],
  verifyGood,
);

// Left column: the fixed blueprint in full, then what the comparison shows.
let y = top;
out.push(header(PAD, y, YAML_W, INK, '#f6f8fa', 'root_agent.yaml', 'an ADK agent config, served by adk run'));
y += 30 + 22;
for (const l of good) {
  out.push(textLine(PAD + 14, y, yamlSpans(l, permit, GREEN), 11));
  y += LINE;
}
y += 20;
const story = [
  `<tspan font-weight="600">Two branches race; the first to finish answers.</tspan>`,
  ``,
  `<tspan fill="${RED}" font-weight="600">✗</tspan> The obvious guard is deadlock-free, and Z3 still finds`,
  `the run in which both commits start while ${code('won')} is empty`,
  `(marking ${busy[1]}). Both land, and two answers leave.`,
  ``,
  `<tspan fill="${GREEN}" font-weight="600">✓</tspan> With one ${code(permit, GREEN)} that each commit must consume,`,
  `only one commit can start. Both claims are proven.`,
  ``,
  `<tspan fill="${MUTED}">Every panel is generated from the two YAML files.</tspan>`,
];
for (const s of story) {
  if (s) out.push(prose(PAD + 14, y, s, 12.5));
  y += s ? LINE + 3 : 8;
}

const H = Math.ceil(Math.max(bottomNaive, bottomGood, y) + PAD);
const body = H - top - 36 - PAD;
out.unshift(
  panel(PAD, top + 36, YAML_W, body),
  panel(X1, top + 36, COL_W, body, '#ffcecb', '#fffafa'),
  panel(X2, top + 36, COL_W, body, '#aceebb', '#fafffb'),
);

const svg = `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-labelledby="hero-title">
<title id="hero-title">A Petri-net blueprint in ADK YAML and two nets side by side. The obvious guard, where won inhibits the commit, is deadlock-free, but Z3 finds both commits starting before either lands, so two answers leave. The fix, where each commit consumes the one permit, proves both claims.</title>
<!-- GENERATED by docs/diagrams/src/hero.ts from hero/*.yaml, hero/verify-*.txt and svg/hero-race*.svg. Do not edit. -->
<rect x="0.5" y="0.5" width="${W - 1}" height="${H - 1}" rx="8" fill="#ffffff" stroke="#d0d7de"/>
${out.join('\n')}
</svg>
`;
writeFileSync(join(ROOT, 'svg', 'hero.svg'), svg);
console.log(`wrote svg/hero.svg (${W}x${H})`);
