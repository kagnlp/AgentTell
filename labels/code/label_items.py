"""Label the released Finding 2 / Finding 3 items with the regex or an LLM judge, and compare.

Runs on the release files alone (f2_items.csv, f3_items.csv, prompts/). Python 3.10+;
the judge needs `pip install anthropic` and ANTHROPIC_API_KEY.

  python label_items.py regex   --finding f2            # regex label per item  -> labels/f2_regex.csv
  python label_items.py judge   --finding f2 [--model M] # LLM judge per item    -> labels/f2_<model>.csv
  python label_items.py compare --finding f2 --a labels/f2_<model>.csv --b labels/f2_regex.csv

Judge protocol: items shuffled with seed 7, batches of 100. System prompt = prompts/<finding>_system.txt
(fixed instruction + rubric). User message = the batch: item id and numbered sentences only, never
another label. Output is JSON validated against prompts/schema.json. Comparison is item level;
weight by the `sessions` column for session-level agreement.
"""
import argparse, csv, json, random, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SEED, BATCH = 7, 100


def items(finding):
    with open(ROOT / f'{finding}_items.csv', encoding='utf-8') as fh:
        return [dict(id=r['item_id'], sessions=int(r['sessions']), sentences=json.loads(r['sentences']))
                for r in csv.DictReader(fh)]


def write(path, rows):
    path.parent.mkdir(exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh); w.writerow(['item_id', 'label', 'sentence']); w.writerows(rows)
    print(len(rows), 'items,', sum(r[1] for r in rows), 'labelled 1 ->', path)


def regex(finding):
    import rules
    f = rules.intent_regex if finding == 'f2' else rules.assurance_regex
    write(HERE / 'labels' / f'{finding}_regex.csv', [(it['id'], f(it['sentences']), '') for it in items(finding)])


def render(batch):
    out = [f'{len(batch)} items follow.', '']
    for it in batch:
        out.append(f'Item {it["id"]}')
        out += [f'  {j}. {s}' for j, s in enumerate(it['sentences'], 1)]
        out.append('')
    return '\n'.join(out)


def judge(finding, model):
    import anthropic
    client = anthropic.Anthropic()
    system = (HERE / 'prompts' / f'{finding}_system.txt').read_text(encoding='utf-8')
    schema = json.loads((HERE / 'prompts' / 'schema.json').read_text(encoding='utf-8'))
    its = items(finding)
    random.Random(SEED).shuffle(its)
    rows = []
    for b in range(0, len(its), BATCH):
        batch = its[b:b + BATCH]
        r = client.messages.create(
            model=model, max_tokens=16000, thinking={'type': 'adaptive'},
            output_config={'effort': 'high', 'format': {'type': 'json_schema', 'schema': schema}},
            system=system, messages=[{'role': 'user', 'content': render(batch)}])
        if r.stop_reason in ('refusal', 'max_tokens'):
            sys.exit(f'batch {b // BATCH}: stop_reason {r.stop_reason}')
        got = {e['item']: e for e in json.loads(next(c.text for c in r.content if c.type == 'text'))['labels']}
        assert set(got) == {it['id'] for it in batch}, f'batch {b // BATCH}: missing or extra items'
        rows += [(i, got[i]['label'], got[i]['sentence'] or '') for i in (it['id'] for it in batch)]
        print('batch', b // BATCH, 'done')
    write(HERE / 'labels' / f'{finding}_{model}.csv', rows)


def compare(finding, a, b):
    from stats import agreement
    w = {it['id']: it['sessions'] for it in items(finding)}
    load = lambda p: {r['item_id']: int(r['label']) for r in csv.DictReader(open(p, encoding='utf-8'))}
    A, B = load(a), load(b)
    ids = sorted(set(A) & set(B))
    print('items   ', json.dumps(agreement([A[i] for i in ids], [B[i] for i in ids])))
    # sessions without an item (no privacy-relevant sentence) are 0 for both labellers
    with open(ROOT / 'session_labels.csv', encoding='utf-8') as fh:
        pop = [r for r in csv.DictReader(fh)
               if r['condition'] == 'loaded' and (finding == 'f2' or r['leak'] == '1')]
    none = sum(not r[f'{finding}_item_id'] for r in pop)
    sa = [A[i] for i in ids for _ in range(w[i])] + [0] * none
    sb = [B[i] for i in ids for _ in range(w[i])] + [0] * none
    print('sessions', json.dumps(agreement(sa, sb)))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['regex', 'judge', 'compare'])
    ap.add_argument('--finding', choices=['f2', 'f3'], required=True)
    ap.add_argument('--model', default='claude-opus-5-5')
    ap.add_argument('--a'); ap.add_argument('--b')
    x = ap.parse_args()
    sys.path.insert(0, str(HERE))
    {'regex': lambda: regex(x.finding), 'judge': lambda: judge(x.finding, x.model),
     'compare': lambda: compare(x.finding, x.a, x.b)}[x.cmd]()
