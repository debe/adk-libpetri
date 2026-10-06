/**
 * The README's opening figure, svg/hero.svg, composed from generated files
 * only, so nothing in it is drawn by hand:
 *
 * - hero/race.yaml and hero/race_broken.yaml: copies of the blueprints in
 *   python/tests/readme_diagrams/hero/, golden-checked by that test;
 * - svg/hero-race.svg: graphviz's render of dot/hero-race.dot, the net that
 *   race.yaml builds, exported by the same test;
 * - hero/verify-*.txt: `adk-libpetri verify` output for both files, verbatim,
 *   cut to verdicts, firings and the last marking of each counterexample.
 *
 * Run after `npm run render` (`npm run build` does both).
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const ROOT = join(import.meta.dirname, '..');
const read = (p: string) => readFileSync(join(ROOT, p), 'utf-8');

const W = 1000;
const PAD = 16;
const LEFT_W = 590;
const LINE = 16.5;
const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
const SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif";
const CHARS = 74; // a 12 px monospace line inside the left column

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

/** Wraps a verify line at " -> " so a long firing sequence stays in the column. */
function wrap(line: string): string[] {
  if (line.length <= CHARS) return [line];
  const indent = ' '.repeat(line.search(/\S/) + 'fires: '.length);
  const out: string[] = [];
  let cur = '';
  for (const part of line.split(' -> ')) {
    const next = cur ? `${cur} -> ${part}` : part;
    if (next.length > CHARS && cur) {
      out.push(`${cur} ->`);
      cur = indent + part;
    } else cur = next;
  }
  out.push(cur);
  return out;
}

type Span = { text: string; fill?: string; weight?: number };

function textLine(x: number, y: number, spans: Span[], size = 12): string {
  const t = spans
    .map((s) => `<tspan${s.fill ? ` fill="${s.fill}"` : ''}${s.weight ? ` font-weight="${s.weight}"` : ''}>${esc(s.text)}</tspan>`)
    .join('');
  return `<text x="${x}" y="${y}" font-family="${MONO}" font-size="${size}" fill="#1f2328" xml:space="preserve">${t}</text>`;
}

/** YAML with keys in blue and the one arc the broken twin drops in red. */
function yamlSpans(line: string, dropped: string | null): Span[] {
  const m = line.match(/^(\s*)([A-Za-z_][\w]*)(:)(.*)$/);
  if (!m) return [{ text: line }];
  const [, ws, key, colon, rest] = m;
  const spans: Span[] = [{ text: ws }, { text: key, fill: '#0550ae' }, { text: colon }];
  if (dropped && rest.includes(dropped)) {
    const i = rest.indexOf(dropped);
    spans.push({ text: rest.slice(0, i) }, { text: dropped, fill: '#cf222e', weight: 700 }, { text: rest.slice(i + dropped.length) });
  } else spans.push({ text: rest });
  return spans;
}

function verifySpans(line: string): Span[] {
  const m = line.match(/^(PROVEN|VIOLATED|UNKNOWN)(\s+)(.*)$/);
  if (!m) return [{ text: line, fill: line.match(/^\d+ proven/) ? '#59636e' : undefined }];
  const fill = m[1] === 'PROVEN' ? '#1a7f37' : m[1] === 'VIOLATED' ? '#cf222e' : '#9a6700';
  return [{ text: m[1], fill, weight: 700 }, { text: m[2] + m[3] }];
}

function panel(x: number, y: number, w: number, h: number, title: string, note = ''): string {
  return [
    `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="6" fill="#f6f8fa" stroke="#d0d7de"/>`,
    `<text x="${x + 14}" y="${y + 22}" font-family="${SANS}" font-size="13" font-weight="600" fill="#1f2328">${esc(title)}`,
    note ? `<tspan font-weight="400" fill="#59636e">  ${esc(note)}</tspan>` : '',
    `</text>`,
  ].join('');
}

/** The rendered net as a nested <svg>, scaled to `width`. */
function nestedNet(svg: string, x: number, y: number, width: number): { body: string; height: number } {
  const root = svg.slice(svg.indexOf('<svg'));
  const vb = root.match(/viewBox="([\d.\s-]+)"/)![1].split(/\s+/).map(Number);
  const height = (width * vb[3]) / vb[2];
  const open = root.match(/<svg[^>]*>/)![0];
  const body = root.replace(open, `<svg x="${x}" y="${y}" width="${width}" height="${height.toFixed(1)}" viewBox="${vb.join(' ')}">`);
  return { body, height };
}

const good = read('hero/race.yaml').trimEnd().split('\n');
const broken = read('hero/race_broken.yaml').trimEnd().split('\n');
/** The word race_broken.yaml drops from a line of race.yaml (the name line aside). */
function dropped(i: number): string | null {
  if (good[i] === broken[i] || good[i].startsWith('name:')) return null;
  const kept = new Set(broken[i].split(/[^\w]+/));
  return good[i].split(/[^\w]+/).find((w) => w && !kept.has(w)) ?? null;
}
const verifyGood = read('hero/verify-race.txt').trimEnd().split('\n');
const verifyBad = read('hero/verify-race_broken.txt').trimEnd().split('\n').flatMap(wrap);

const out: string[] = [];
let y = PAD;

// Left, top: the blueprint.
const yamlH = 34 + good.length * LINE + 10;
out.push(panel(PAD, y, LEFT_W, yamlH, 'root_agent.yaml', 'an ADK agent config; adk run and adk web serve it'));
good.forEach((l, i) => out.push(textLine(PAD + 14, y + 44 + i * LINE, yamlSpans(l, dropped(i)))));
y += yamlH + 12;

// Left, bottom: what verify says about it, and about the twin without the red arc.
const NET_X = PAD + LEFT_W + 14;
const netW = W - NET_X - PAD;
const net = nestedNet(read('svg/hero-race.svg'), NET_X + 8, PAD + 34, netW - 16);
const vH = Math.max(34 + (verifyGood.length + 1 + verifyBad.length) * LINE + 34, net.height + 44 - yamlH - 12);
out.push(panel(PAD, y, LEFT_W, vH, '$ adk-libpetri verify root_agent.yaml'));
let vy = y + 44;
for (const l of verifyGood) { out.push(textLine(PAD + 14, vy, verifySpans(l))); vy += LINE; }
vy += 10;
out.push(`<text x="${PAD + 14}" y="${vy}" font-family="${SANS}" font-size="13" font-weight="600" fill="#1f2328">Remove the red <tspan font-family="${MONO}" fill="#cf222e">${esc(good.map((_, i) => dropped(i)).find(Boolean)!)}</tspan> arc, and both branches can answer:</text>`);
vy += LINE + 6;
for (const l of verifyBad) { out.push(textLine(PAD + 14, vy, verifySpans(l))); vy += LINE; }
const leftBottom = y + vH;

// Right: the net, as the diagrams render it.
const netH = Math.max(net.height + 44, leftBottom - PAD);
out.unshift(panel(NET_X, PAD, netW, netH, 'the net it builds'));
out.push(net.body);

const H = Math.ceil(Math.max(leftBottom, PAD + netH) + PAD);
const svg = `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-labelledby="hero-title">
<title id="hero-title">A Petri-net blueprint in ADK YAML, the net it builds, and adk-libpetri verify: both claims proven, and with one arc removed, a counterexample in which two branches both answer</title>
<!-- GENERATED by docs/diagrams/src/hero.ts from hero/*.yaml, hero/verify-*.txt and svg/hero-race.svg. Do not edit. -->
<rect x="0.5" y="0.5" width="${W - 1}" height="${H - 1}" rx="8" fill="#ffffff" stroke="#d0d7de"/>
${out.join('\n')}
</svg>
`;
writeFileSync(join(ROOT, 'svg', 'hero.svg'), svg);
console.log(`wrote svg/hero.svg (${W}x${H})`);
