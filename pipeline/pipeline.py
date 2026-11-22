#!/usr/bin/env python3
import csv, glob, json, os, re, time, sys, subprocess
from pathlib import Path
import duckdb

DATA = Path(os.environ.get('DATA_DIR', '/data'))
OUT = Path(os.environ.get('OUTPUT_DIR', '/output'))
SCRATCH = Path(os.environ.get('SCRATCH_DIR', '/scratch'))
SCRATCH.mkdir(parents=True, exist_ok=True)
con = duckdb.connect()
con.execute(f"SET threads = {int(os.environ.get('NCPU', '4'))}")
con.execute(f"SET memory_limit = '{max(1,int(int(os.environ.get('MEM_GB','8'))*.8))}GB'")
def lit(x): return "'" + str(x).replace("'", "''") + "'"
con.execute('SET temp_directory = '+lit(SCRATCH/'duckdb_tmp'))
con.execute('SET preserve_insertion_order = false')
con.execute("SET TimeZone='UTC'")

def q(sql):
    started = time.perf_counter()
    try:
        return con.execute(sql)
    finally:
        statement = sql.strip().replace('\n', ' ')
        label = statement[:125]
        print(f"[TIMING] SQL {time.perf_counter()-started:.3f}s | {label}", file=sys.stderr, flush=True)

def _clean(kind, cols):
    native = Path('/app/csv_cleaner')
    if native.is_file():
        dst = SCRATCH/(kind+'_clean.csv')
        subprocess.run([str(native), str(DATA/kind), str(dst), ','.join(cols), str(len(cols))], check=True)
        schema = '{'+','.join(lit(c)+": 'VARCHAR'" for c in cols)+'}'
    return f"read_csv({lit(dst)}, columns={schema}, header=true, delim=',', quote='\"', escape='\"', auto_detect=false, nullstr='', allow_quoted_nulls=true, new_line='\\n')"
    # The contest dialect does not allow embedded commas, quotes or LF.
    # Validate before removing syntactic quotes. Preserve CR as field content.
    field = rb'(?:[^,"\n]*|"[^,"\n]*")'
    pat = re.compile(rb','.join([field]*len(cols)))
    dst = SCRATCH/(kind+'_clean.csv')
    with dst.open('wb', buffering=1024*1024) as w:
        w.write((','.join(cols)+'\n').encode())
        for src in sorted((DATA/kind).glob('*.csv')):
            with src.open('rb', buffering=1024*1024) as r:
                next(r,None)
                for line in r:
                    line = line.removesuffix(b'\n')
                    # Fast path: unquoted rows need only the expected comma count.
                    # The regex still validates quoted rows and malformed quoting.
                    if b'"' in line:
                        if not pat.fullmatch(line):
                            continue
                        line = line.replace(b'"', b'')
                    elif line.count(b',') != len(cols) - 1:
                        continue
                    if b'\r' in line:
                        line = b','.join(b'"'+v+b'"' for v in line.split(b','))
                    w.write(line+b'\n')
    schema = '{'+','.join(lit(c)+": 'VARCHAR'" for c in cols)+'}'
    return f"read_csv({lit(dst)}, columns={schema}, header=true, delim=',', quote='\"', escape='\"', auto_detect=false, nullstr='', allow_quoted_nulls=true, new_line='\\n')"

def clean(kind, cols):
    started = time.perf_counter()
    try:
        return _clean(kind, cols)
    finally:
        print(f"[TIMING] CSV clean {kind}: {time.perf_counter()-started:.3f}s", file=sys.stderr, flush=True)

def dim(name, cols):
    schema = '{'+','.join(lit(c)+": 'VARCHAR'" for c in cols)+'}'
    return f"read_csv({lit(DATA/(name+'.csv'))}, columns={schema}, header=true, auto_detect=false, delim=',', quote='\"', escape='\"')"

link_started = time.perf_counter()
parent = {}
def root(a):
    while parent[a] != a:
        parent[a] = parent[parent[a]]
        a = parent[a]
    return a
for src in sorted((DATA/'links').glob('*.csv')):
    with src.open('rb') as f:
        next(f,None)
        for line in f:
            line=line.removesuffix(b'\n')
            if not re.fullmatch(rb'(?:[^,"\n]*|"[^,"\n]*"),(?:[^,"\n]*|"[^,"\n]*")',line): continue
            a,b=line.replace(b'"',b'').split(b',')
            if not a or not b: continue
            parent.setdefault(a,a); parent.setdefault(b,b)
            a,b=root(a),root(b)
            if a!=b: parent[max(a,b)]=min(a,b)
mp=SCRATCH/'identity.csv'
with mp.open('w',encoding='utf-8',newline='') as f:
    w=csv.writer(f,lineterminator='\n',quoting=csv.QUOTE_ALL)
    w.writerow(['uid','person'])
    for a in parent:
        b=root(a)
        if a!=b: w.writerow([a.decode(),b.decode()])
parent.clear()
print(f"[TIMING] Union-find links: {time.perf_counter()-link_started:.3f}s", file=sys.stderr, flush=True)
q(f"CREATE TABLE identities AS SELECT * FROM read_csv({lit(mp)}, columns={{'uid':'VARCHAR','person':'VARCHAR'}}, header=true, auto_detect=false, delim=',', quote='\"', escape='\"', new_line='\\n')")
mp.unlink()
manifest=json.loads((DATA/'manifest.json').read_text())
start,end=[q('SELECT epoch_ms(CAST('+lit(manifest[k])+ ' AS TIMESTAMPTZ))').fetchone()[0] for k in ('window_start','window_end')]
q(r"""CREATE MACRO ts_ms(s) AS CASE
 WHEN regexp_full_match(s,'[0-9]+') THEN try_cast(ltrim(s,'0') AS BIGINT) + 0
 WHEN regexp_full_match(s,'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z') THEN epoch_ms(try_strptime(substr(s,1,19),'%Y-%m-%dT%H:%M:%S'))
 WHEN regexp_full_match(s,'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-]([01][0-9]|2[0-3]):[0-5][0-9]') THEN epoch_ms(try_strptime(substr(s,1,19),'%Y-%m-%dT%H:%M:%S')) - (CASE WHEN substr(s,20,1)='+' THEN 1 ELSE -1 END)*(try_cast(substr(s,21,2) AS BIGINT)*60+try_cast(substr(s,24,2) AS BIGINT))*60000
 ELSE NULL END""")
# Explicit calendar-time checks guard against parsers normalizing leap seconds etc.
q(r"""CREATE MACRO valid_ts(s) AS CASE WHEN regexp_full_match(s,'[0-9]+') THEN coalesce(try_cast(nullif(ltrim(s,'0'),'') AS BIGINT), CASE WHEN regexp_full_match(s,'0+') THEN 0 END)
 WHEN try_cast(substr(s,12,2) AS INTEGER) BETWEEN 0 AND 23 AND try_cast(substr(s,15,2) AS INTEGER) BETWEEN 0 AND 59 AND try_cast(substr(s,18,2) AS INTEGER) BETWEEN 0 AND 59 THEN ts_ms(s) END""")
e=clean('events',['event_id','user_id','ts','ingest_ts','event_type','product_id','price_cents'])
q(f"""CREATE TABLE events AS WITH dedup AS (
 SELECT * FROM {e} WHERE event_id IS NOT NULL QUALIFY row_number() OVER(PARTITION BY event_id ORDER BY ingest_ts)=1
), parsed AS (SELECT *, valid_ts(ts) AS t, valid_ts(ingest_ts) AS ing, CASE WHEN regexp_full_match(price_cents,'[0-9]+') THEN try_cast(ltrim(price_cents,'0') AS BIGINT) END AS price FROM dedup)
SELECT coalesce(i.person,p.user_id) AS person,t,product_id,CASE WHEN event_type='purchase' THEN price ELSE 0 END AS revenue
FROM parsed p LEFT JOIN identities i ON p.user_id=i.uid
WHERE p.user_id IS NOT NULL AND t >= {start} AND t < {end} AND ing-t<=86400000 AND product_id IS NOT NULL AND event_type IN ('view','cart','purchase') AND (event_type!='purchase' OR price BETWEEN 1 AND 10000000)""")
(SCRATCH/'events_clean.csv').unlink()
q("""CREATE TABLE session_events AS WITH gaps AS (SELECT *,lag(t) OVER(PARTITION BY person ORDER BY t) AS prev FROM events)
SELECT person,t,product_id,revenue,max(CASE WHEN prev IS NULL OR t-prev>1800000 THEN t END) OVER(PARTITION BY person ORDER BY t RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS s FROM gaps""")
q('DROP TABLE events')
q("""CREATE TABLE sessions AS SELECT person,s,CAST(make_timestamp_ms(s) AS DATE) AS day,count(*)::BIGINT AS events,sum(revenue)::BIGINT AS revenue FROM session_events GROUP BY person,s""")
q(f"CREATE TABLE products AS SELECT * FROM {dim('products',['product_id','category'])}")
q("""CREATE TABLE topcats AS WITH totals AS (SELECT person,CAST(make_timestamp_ms(s) AS DATE) AS day,coalesce(category,'unknown') AS category,sum(revenue) AS total FROM session_events e LEFT JOIN products p USING(product_id) WHERE revenue>0 GROUP BY 1,2,3) SELECT person,day,category FROM totals QUALIFY row_number() OVER(PARTITION BY person,day ORDER BY total DESC,category)=1""")
q('DROP TABLE session_events')
for t in ('user_daily','campaign_daily'): (OUT/t).mkdir(parents=True,exist_ok=True)
q(f"""COPY (SELECT person AS user_id,day,count(*)::BIGINT AS sessions,sum(events)::BIGINT AS events,sum(revenue)::BIGINT AS revenue_cents,category AS top_category FROM sessions LEFT JOIN topcats USING(person,day) GROUP BY person,day,category) TO {lit(OUT/'user_daily'/'part-0.parquet')} (FORMAT parquet)""")
q(f"CREATE TABLE campaigns AS SELECT * FROM {dim('campaigns',['campaign_id','channel'])}")
c=clean('clicks',['click_id','user_id','campaign_id','ts'])
q(f"""CREATE TABLE clicks AS WITH dedup AS (SELECT * FROM {c} WHERE click_id IS NOT NULL QUALIFY row_number() OVER(PARTITION BY click_id)=1), parsed AS (SELECT *,valid_ts(ts) AS t FROM dedup) SELECT coalesce(i.person,p.user_id) AS person,p.campaign_id,t,click_id FROM parsed p JOIN campaigns c USING(campaign_id) LEFT JOIN identities i ON p.user_id=i.uid WHERE p.user_id IS NOT NULL AND t >= {start-604800000} AND t < {end}""")
(SCRATCH/'clicks_clean.csv').unlink()
q("""CREATE TABLE latest AS SELECT person,t,campaign_id FROM clicks QUALIFY row_number() OVER(PARTITION BY person,t ORDER BY click_id)=1""")
q("""CREATE TABLE credited AS SELECT CASE WHEN l.t IS NULL OR s.s-l.t>604800000 THEN 'organic' ELSE l.campaign_id END AS campaign_id,s.day,count(*)::BIGINT AS sessions,count(*) FILTER(WHERE s.revenue>0)::BIGINT AS purchasing_sessions,sum(s.revenue)::BIGINT AS revenue_cents FROM sessions s ASOF LEFT JOIN latest l ON s.person=l.person AND s.s>=l.t GROUP BY 1,2""")
q('DROP TABLE latest')
# Merge overlapping 7-day intervals per person/campaign to avoid duplicate assists.
q("""CREATE TABLE intervals AS WITH gaps AS (SELECT *,lag(t) OVER(PARTITION BY person,campaign_id ORDER BY t) AS prev FROM clicks), marks AS (SELECT *,max(CASE WHEN prev IS NULL OR t-prev>604800000 THEN t END) OVER(PARTITION BY person,campaign_id ORDER BY t RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS a FROM gaps) SELECT person,campaign_id,a,max(t)+604800000 AS b FROM marks GROUP BY 1,2,3""")
q('DROP TABLE clicks')
q("""CREATE TABLE assists AS SELECT i.campaign_id,s.day,count(*)::BIGINT AS assisted_sessions FROM sessions s JOIN intervals i ON s.person=i.person AND s.s>=i.a AND s.s<=i.b GROUP BY 1,2""")
q(f"""COPY (SELECT campaign_id,coalesce(channel,'organic') AS channel,day,coalesce(sessions,0)::BIGINT AS sessions,coalesce(purchasing_sessions,0)::BIGINT AS purchasing_sessions,coalesce(revenue_cents,0)::BIGINT AS revenue_cents,coalesce(assisted_sessions,0)::BIGINT AS assisted_sessions FROM credited FULL OUTER JOIN assists USING(campaign_id,day) LEFT JOIN campaigns USING(campaign_id)) TO {lit(OUT/'campaign_daily'/'part-0.parquet')} (FORMAT parquet)""")
print('Wrote both output tables')
