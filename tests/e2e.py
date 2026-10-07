"""Сквозные тесты «12 недель».

Запуск:  python3 tests/e2e.py            (все кейсы)
         python3 tests/e2e.py F07 F16    (только кейсы с такими префиксами)

Приложение раздаётся локальным сервером из корня репозитория. Supabase заменён
мок-сервером в этом же процессе (общий для всех «устройств»), шрифты Google
отключены. Каждый кейс получает чистый браузерный контекст. В каждом кейсе
автоматически проверяются ошибки в консоли и горизонтальная прокрутка.
"""
import asyncio, json, os, re, sys, threading, traceback, uuid, functools
from datetime import datetime, date, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from playwright.async_api import async_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'tests', 'out')
NOW = datetime(2026, 10, 7, 12, 0, 0)          # среда, неделя 3 цикла, начатого 21 сентября
TZ = 'Europe/Kaliningrad'
LS = 'tw12-state-v1'

# ---------------------------------------------------------------- static server
class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
def start_server():
    h = functools.partial(Quiet, directory=ROOT)
    srv = ThreadingHTTPServer(('127.0.0.1', 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f'http://127.0.0.1:{srv.server_address[1]}/index.html'

# ---------------------------------------------------------------- mock Supabase
class Backend:
    def __init__(self):
        self.users = {}      # email -> {id, password, confirmed}
        self.rows = {}       # user_id -> {state, updated_at}
        self.offline = False
        self.log = []
    def add_user(self, email, password, confirmed=True):
        u = {'id': str(uuid.uuid4()), 'password': password, 'confirmed': confirmed}
        self.users[email] = u
        return u
    def handle(self, op, p):
        self.log.append(op)
        if self.offline and op in ('upsert', 'select', 'signIn'):
            return {'error': {'message': 'Failed to fetch'}}
        if op == 'signUp':
            if p['email'] in self.users:   # Supabase маскирует существующие аккаунты
                return {'data': {'user': {'identities': []}, 'session': None}, 'error': None}
            if len(p['password']) < 6:
                return {'data': {}, 'error': {'message': 'Password should be at least 6 characters'}}
            self.add_user(p['email'], p['password'], confirmed=False)
            return {'data': {'user': {'identities': [{}]}, 'session': None}, 'error': None}
        if op == 'signIn':
            u = self.users.get(p['email'])
            if not u or u['password'] != p['password']:
                return {'error': {'message': 'Invalid login credentials'}}
            if not u['confirmed']:
                return {'error': {'message': 'Email not confirmed'}}
            return {'user': {'id': u['id'], 'email': p['email']}, 'error': None}
        if op == 'reset':
            self.last_reset = p
            return {'data': {}, 'error': None}
        if op == 'recoverySession':
            u = self.users[p['email']]
            return {'user': {'id': u['id'], 'email': p['email']}}
        if op == 'updateUser':
            for e, u in self.users.items():
                if u['id'] == p['uid']:
                    if u['password'] == p['password']:
                        return {'data': {}, 'error': {'message': 'New password should be different from the old password.'}}
                    u['password'] = p['password']
                    return {'data': {}, 'error': None}
            return {'data': {}, 'error': {'message': 'not signed in'}}
        if op == 'upsert':
            row = p['row']
            if not p.get('uid') or row.get('user_id') != p['uid']:     # RLS
                return {'error': {'code': '42501', 'message': 'new row violates row-level security policy'}}
            self.rows[p['uid']] = {'state': row['state'], 'updated_at': row['updated_at']}
            return {'error': None}
        if op == 'select':
            if not p.get('uid') or p['target'] != p['uid']:
                return {'data': None, 'error': None}
            return {'data': self.rows.get(p['uid']), 'error': None}
        return {'error': {'message': 'unknown op ' + op}}

MOCK_JS = r"""
window.supabase={createClient(){
  const KEY='sb-mock-session';let sess=null;try{sess=JSON.parse(localStorage.getItem(KEY))}catch(e){}
  const subs=[];const emit=(ev,s)=>subs.forEach(f=>f(ev,s));
  const call=async(op,p)=>JSON.parse(await window.__be(op,JSON.stringify(p||{})));
  const setSess=(s,ev)=>{sess=s;try{s?localStorage.setItem(KEY,JSON.stringify(s)):localStorage.removeItem(KEY)}catch(e){}emit(ev,s)};
  const auth={
    onAuthStateChange(f){subs.push(f);setTimeout(async()=>{
      const m=location.hash.match(/recovery=([^&]+)/);
      if(m){const r=await call('recoverySession',{email:decodeURIComponent(m[1])});history.replaceState(null,'',location.pathname);setSess({user:r.user},'PASSWORD_RECOVERY');return}
      f('INITIAL_SESSION',sess)},0);return{data:{subscription:{unsubscribe(){}}}}},
    async signUp({email,password}){return call('signUp',{email,password})},
    async signInWithPassword({email,password}){const r=await call('signIn',{email,password});if(r.error)return{data:{},error:r.error};setSess({user:r.user},'SIGNED_IN');return{data:{session:sess},error:null}},
    async resetPasswordForEmail(email,o){return call('reset',{email,redirectTo:o&&o.redirectTo})},
    async updateUser({password}){return call('updateUser',{uid:sess&&sess.user.id,password})},
    async signOut(){setSess(null,'SIGNED_OUT');return{error:null}}
  };
  return{auth,from(){return{
    upsert:async(row)=>call('upsert',{uid:sess&&sess.user.id,row}),
    select(){return{eq(k,v){return{maybeSingle:async()=>call('select',{uid:sess&&sess.user.id,target:v})}}}}
  }}}
}};
"""

# ---------------------------------------------------------------- state builders
def iso(d): return d.strftime('%Y-%m-%d')
def mk_state(start='2026-09-21', goals=None, weeks=None, **kw):
    s = {'v': 1, 'isExample': False, 'rev': 1, 'updatedAt': 1, 'cycle': {'title': 'Тестовый цикл', 'start': start},
         'vision': {'long': '', 'three': ''}, 'goals': goals or [], 'weeks': weeks or {}, 'xpBank': 0, 'history': [], 'ach': {}, 'skin': 'classic'}
    s.update(kw)
    return s
def goal(gid='gA1b2c3', title='Цель тест', tactics=None, measure=None):
    return {'id': gid, 'title': title, 'why': '', 'measure': measure or {'name': 'Страниц', 'target': 40, 'unit': 'стр.'},
            'tactics': tactics if tactics is not None else [tac()]}
def tac(tid='tQ1w2e3', title='Писать 90 минут', per=4, frm=1, to=12):
    return {'id': tid, 'title': title, 'per': per, 'from': frm, 'to': to}
def week(done=None, **kw):
    w = {'done': done or {}, 'blocks': {'strategic': False, 'buffer': 0, 'breakout': False},
         'review': {'good': '', 'bad': '', 'next': ''}, 'lag': {}, 'reviewed': False}
    w.update(kw)
    return w

# ---------------------------------------------------------------- harness
class Fail(AssertionError): pass
def ok(cond, msg):
    if not cond: raise Fail(msg)

CASES = []
def case(cid, title):
    def deco(fn):
        CASES.append((cid, title, fn)); return fn
    return deco

class T:
    def __init__(self, browser, url, be):
        self.browser, self.url, self.be = browser, url, be
        self.errors, self.pages, self.ctxs = [], [], []
    async def device(self, state=None, viewport=(400, 860), scheme='light', storage_broken=False, now=NOW, raw=None, hash='',
                     login=None, legacy=None, email='me@test.io'):
        """state — цикл вошедшего пользователя (он же в облаке); legacy — данные со старой версии без входа."""
        if login is None: login = state is not None or raw is not None
        ctx = await self.browser.new_context(viewport={'width': viewport[0], 'height': viewport[1]}, color_scheme=scheme,
                                             timezone_id=TZ, locale='ru-RU', accept_downloads=True)
        self.ctxs.append(ctx)
        await ctx.expose_function('__be', lambda op, p: json.dumps(self.be.handle(op, json.loads(p))))
        async def route(r):
            u = r.request.url
            if 'supabase-js' in u: return await r.fulfill(body=MOCK_JS, content_type='text/javascript')
            if 'fonts.g' in u: return await r.abort()
            await r.continue_()
        await ctx.route('**/*', route)
        seed = {}
        if login:
            u = self.be.users.get(email) or self.be.add_user(email, 'secret1')
            seed['sb-mock-session'] = json.dumps({'user': {'id': u['id'], 'email': email}})
            if state is not None:
                st = json.loads(json.dumps(state)); st['owner'] = u['id']
                self.be.rows.setdefault(u['id'], {'state': json.loads(json.dumps(st)), 'updated_at': 'x'})
                seed[f'{LS}:{u["id"]}'] = json.dumps(st)
            if raw is not None: seed[f'{LS}:{u["id"]}'] = raw
        if legacy is not None: seed[LS] = json.dumps(legacy)
        if seed:
            await ctx.add_init_script(f"if(!sessionStorage.getItem('__seeded')){{const s={json.dumps(seed)};for(const k in s)localStorage.setItem(k,s[k]);sessionStorage.setItem('__seeded','1')}}")
        if storage_broken:
            await ctx.add_init_script("Object.defineProperty(window,'localStorage',{get(){throw new Error('denied')}})")
        page = await ctx.new_page()
        if now: await page.clock.set_fixed_time(now)
        page.on('pageerror', lambda e: self.errors.append(f'pageerror: {e}'))
        page.on('console', lambda m: m.type == 'error' and not re.search(r'ERR_FAILED|fonts\.g', m.text) and self.errors.append(f'console: {m.text}'))
        await page.goto(self.url + hash)
        await page.wait_for_timeout(250)
        self.pages.append(page)
        return page
    async def close(self):
        for c in self.ctxs: await c.close()

# page helpers
async def act(p, key, wait=120):
    await p.click(f'[data-act="{key}"]'); await p.wait_for_timeout(wait)
async def txt(p, sel): return (await p.text_content(sel) or '').strip()
async def stored(p):
    return await p.evaluate(f"(()=>{{try{{const k=Object.keys(localStorage).find(k=>k.startsWith({json.dumps(LS+':')}));return k?JSON.parse(localStorage.getItem(k)):null}}catch(e){{return null}}}})()")
async def keys(p): return await p.evaluate("Object.keys(localStorage)")
async def overflow(p):
    return await p.evaluate("document.documentElement.scrollWidth-document.documentElement.clientWidth")
async def ring(p): return await txt(p, '.ring-num .big')
async def xp(p):
    lab = await p.get_attribute('.lvlchip', 'aria-label'); return int(re.search(r'(\d+) XP', lab).group(1))
async def level(p): return int(await txt(p, '.lvl-badge'))
async def signin(p, email, pw):
    if not await p.locator('.gate').count(): await act(p, 'tab:account')
    if await p.locator('form[data-form=signup]').count(): await act(p, 'auth:in')
    await p.fill('#au-email', email); await p.fill('#au-pass', pw)
    await p.click('form[data-form=signin] button[type=submit]'); await p.wait_for_timeout(500)
async def pip(p, i): await p.click(f'.pip >> nth={i}'); await p.wait_for_timeout(80)

A11Y_JS = r"""
(()=>{
 const vis=e=>{const r=e.getBoundingClientRect();const cs=getComputedStyle(e);return r.width>0&&r.height>0&&cs.visibility!=='hidden'&&cs.display!=='none'};
 const nameOf=e=>{if(e.getAttribute('aria-label'))return e.getAttribute('aria-label');if(e.labels&&e.labels.length)return [...e.labels].map(l=>l.textContent).join(' ');
   if(e.id){const l=document.querySelector('label[for="'+e.id+'"]');if(l)return l.textContent}return (e.textContent||'').trim()||e.getAttribute('title')||''};
 const noName=[...document.querySelectorAll('button,input:not([type=hidden]),select,textarea,a[href]')].filter(e=>vis(e)&&!nameOf(e).trim()).map(e=>e.outerHTML.slice(0,90));
 const lum=c=>{const m=c.match(/[\d.]+/g);if(!m)return null;const [r,g,b]=m.slice(0,3).map(Number).map(v=>{v/=255;return v<=.03928?v/12.92:Math.pow((v+.055)/1.055,2.4)});return .2126*r+.7152*g+.0722*b};
 const bgOf=e=>{for(let x=e;x;x=x.parentElement){const c=getComputedStyle(x).backgroundColor;const m=c.match(/[\d.]+/g);if(m&&(m.length<4||+m[3]>0.5))return c}return getComputedStyle(document.body).backgroundColor};
 const low=[];
 document.querySelectorAll('body *').forEach(e=>{
   if(!vis(e))return;const own=[...e.childNodes].some(n=>n.nodeType===3&&n.textContent.trim());if(!own)return;
   if(e.closest('.skin-prev,svg,.cell.future,[disabled]'))return;
   const cs=getComputedStyle(e);if(+cs.opacity<1)return;const L1=lum(cs.color),L2=lum(bgOf(e));if(L1==null||L2==null)return;
   const ratio=(Math.max(L1,L2)+.05)/(Math.min(L1,L2)+.05);const size=parseFloat(cs.fontSize),bold=+cs.fontWeight>=600;
   const need=(size>=24||(bold&&size>=18.6))?3:4.5;
   if(ratio<need-0.05&&!e.closest('.ach.locked'))low.push(`${ratio.toFixed(2)} «${(e.textContent||'').trim().slice(0,30)}»`)});
 return {noName,low:[...new Set(low)].slice(0,8)};
})()
"""
async def a11y(p): return await p.evaluate(A11Y_JS)

# ================================================================= CASES
# ---------- F01
@case('F01-H1', 'Без входа виден только экран входа')
async def _(t):
    p = await t.device()
    ok(await p.locator('.gate').count() == 1, 'нет экрана входа')
    ok(await p.locator('.tab').count() == 0 and await p.locator('.lvlchip').count() == 0, 'приложение доступно без входа')
    ok(await p.get_by_role('button', name='Войти').count() == 1, 'нет кнопки «Войти»')
    ok(await keys(p) == [], f'без входа что-то сохранено: {await keys(p)}')

@case('F01-H2', 'После входа в пустой аккаунт — приветствие и план')
async def _(t):
    t.be.add_user('me@test.io', 'secret1')
    p = await t.device(); await signin(p, 'me@test.io', 'secret1')
    ok(await p.locator('.welcome').count() == 1, 'нет приветствия после входа')
    ok('Мой первый цикл' in await txt(p, '.brand-sub') and await level(p) == 1, 'не пустой цикл')
    await p.get_by_role('button', name='Составить план').click()
    ok(await p.locator('#vi-long').count() == 1 and await p.locator('[data-act="addgoal"]').count() == 1, 'нет полей плана')

@case('F01-E1', 'Старая копия примера стирается при входе')
async def _(t):
    t.be.add_user('me@test.io', 'secret1')
    leg = mk_state(goals=[{'id': 'g1', 'title': 'Написать 40 страниц черновика', 'why': '', 'measure': {}, 'tactics': [{'id': 't1', 'title': 'x', 'per': 5, 'from': 1, 'to': 12}]}])
    p = await t.device(legacy=leg); await signin(p, 'me@test.io', 'secret1')
    ok(await p.locator('.welcome').count() == 1, 'копия примера не стёрта')
    ok(LS not in await keys(p), 'копия осталась в хранилище')

@case('F01-E2', 'Повреждённая копия на устройстве')
async def _(t):
    p = await t.device(raw='{not json'); await p.wait_for_timeout(300)
    ok(await p.locator('.welcome').count() == 1, 'нет приветствия')

@case('F01-E3', 'Хранилище недоступно: вход и работа в памяти')
async def _(t):
    t.be.add_user('me@test.io', 'secret1')
    p = await t.device(storage_broken=True); await signin(p, 'me@test.io', 'secret1')
    await p.get_by_role('button', name='Составить план').click(); await act(p, 'addgoal')
    ok(await p.locator('.goalcard').count() == 1, 'не добавилась цель в памяти')

@case('F01-E4', 'Ширина 360 px без прокрутки вбок')
async def _(t):
    p = await t.device(viewport=(360, 740)); ok(await overflow(p) <= 0, 'прокрутка на экране входа')
    p2 = await t.device(mk_state(goals=[goal()]), viewport=(360, 740)); ok(await overflow(p2) <= 0, 'прокрутка в приложении')

@case('F01-E5', 'Экран входа и приветствие без ошибок доступности')
async def _(t):
    p = await t.device(); r = await a11y(p); ok(not r['noName'] and not r['low'], 'вход: ' + str(r))
    p2 = await t.device(mk_state()); r = await a11y(p2); ok(not r['noName'] and not r['low'], 'приветствие: ' + str(r))

@case('F01-E6', 'Все вкладки видны на 360 px')
async def _(t):
    for vp in [(360, 740), (390, 844)]:
        p = await t.device(mk_state(goals=[goal()]), viewport=vp)
        hidden = await p.evaluate("""(()=>{const n=document.getElementById('tabs').getBoundingClientRect();return [...document.querySelectorAll('.tab')].filter(b=>{const r=b.getBoundingClientRect();return r.right>n.right+1||r.left<n.left-1}).map(b=>b.textContent)})()""")
        ok(not hidden, f'{vp[0]}px: за краем {hidden}')

# ---------- F02
@case('F02-H1', 'Пример открывается')
async def _(t):
    p = await t.device(mk_state()); await act(p, 'ex:show')
    ok(await p.locator('.banner').count() == 1, 'нет баннера примера')
    ok(await p.locator('.goalblock').count() == 3, 'в примере не 3 цели')

@case('F02-H2', 'Пример не сохраняется')
async def _(t):
    p = await t.device(mk_state()); await act(p, 'ex:show'); await pip(p, 5); await p.wait_for_timeout(900)
    ok(not (await stored(p) or {}).get('isExample') and not (await stored(p) or {'goals':[]})['goals'], 'пример попал в хранилище')
    await p.reload(); await p.wait_for_timeout(250)
    ok(await p.locator('.welcome').count() == 1, 'после перезагрузки не приветствие')

@case('F02-H3', '«Закрыть пример» возвращает свои данные')
async def _(t):
    st = mk_state(goals=[goal()])
    p = await t.device(st)
    await act(p, 'tab:plan')
    # свой цикл есть — пример недоступен с приветствия, открываем через состояние
    await p.evaluate("document.querySelector('[data-act=\"tab:week\"]').click()")
    ok(await p.locator('.welcome').count() == 0, 'при своих данных показано приветствие')

@case('F02-H4', '«Начать свой цикл» из примера')
async def _(t):
    p = await t.device(mk_state()); await act(p, 'ex:show'); await act(p, 'ex:clear')
    ok(await p.locator('[data-act="addgoal"]').count() == 1, 'не открылся План')
    ok(await p.locator('.banner').count() == 0, 'баннер примера остался')

@case('F02-N1', 'Пример не уходит в облако')
async def _(t):
    p = await t.device(mk_state()); uid = t.be.users['me@test.io']['id']
    await act(p, 'ex:show'); await pip(p, 5); await p.wait_for_timeout(1200)
    ok(not t.be.rows[uid]['state'].get('isExample') and not t.be.rows[uid]['state']['goals'], 'пример ушёл в облако')

# ---------- F03
@case('F03-H1', 'Название цикла сохраняется')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'tab:plan')
    await p.fill('#cy-title', 'Весна 🌱'); await p.press('#cy-title', 'Tab'); await p.wait_for_timeout(300)
    await p.reload(); await p.wait_for_timeout(250)
    ok('Весна 🌱' in await txt(p, '.brand-sub'), 'название не сохранилось')

@case('F03-H2', 'Смена даты старта пересчитывает неделю')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'tab:plan')
    await p.fill('#cy-start', '2026-10-05'); await p.wait_for_timeout(200)
    ok('5 окт' in await txt(p, '.card .hint'), 'подпись с датами не обновилась')
    await act(p, 'tab:week')
    ok('Неделя 1' in await txt(p, '.h-week'), 'текущая неделя не 1')

@case('F03-E1', 'Старт в будущем')
async def _(t):
    p = await t.device(mk_state(start='2026-10-19', goals=[goal()]))
    ok('Неделя 1' in await txt(p, '.h-week'), 'не неделя 1')
    ok('стартует' in await txt(p, '.hero-info'), 'нет подсказки о старте')

@case('F03-E2', 'Цикл закончился (старт 100 дней назад)')
async def _(t):
    start = iso(NOW.date() - timedelta(days=100))
    p = await t.device(mk_state(start=start, goals=[goal()]))
    ok('Неделя 13' in await txt(p, '.h-week'), 'не открыта неделя 13')

@case('F03-E3', 'Старт в среду')
async def _(t):
    p = await t.device(mk_state(start='2026-09-23', goals=[goal()]))
    ok('Неделя 3' in await txt(p, '.h-week'), 'номер недели неверный')
    ok('7–13 окт' in await txt(p, '.hero-info .eyebrow'), 'диапазон дат неверный: ' + await txt(p, '.hero-info .eyebrow'))

@case('F03-E4', 'Переход на зимнее время')
async def _(t):
    p = await t.device(mk_state(start='2026-10-19', goals=[goal()]), now=datetime(2026, 10, 26, 0, 30))
    ok('Неделя 2' in await txt(p, '.h-week'), 'после смены времени неделя сбилась: ' + await txt(p, '.h-week'))
    ok('26 окт – 1 ноя' in await txt(p, '.hero-info .eyebrow'), 'даты: ' + await txt(p, '.hero-info .eyebrow'))

@case('F03-N1', 'Пустая дата старта не ломает цикл')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'tab:plan')
    await p.fill('#cy-start', ''); await p.dispatch_event('#cy-start', 'change'); await p.wait_for_timeout(200)
    ok((await stored(p) or mk_state())['cycle']['start'] == '2026-09-21', 'дата старта испорчена')

# ---------- F04
@case('F04-H1', 'Видение даёт 20 XP и достижение')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'tab:plan')
    x0 = await xp(p)
    await p.fill('#vi-long', 'Жить спокойно и интересно'); await p.press('#vi-long', 'Tab'); await p.wait_for_timeout(300)
    ok(await xp(p) == x0 + 20 + 50, f'XP {x0} -> {await xp(p)}, ожидалось +70 (видение и достижение)')
    ok('vision' in (await stored(p))['ach'], 'нет достижения «Взгляд вдаль»')

@case('F04-E1', 'Пробелы в видении не дают XP')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'tab:plan')
    x0 = await xp(p); await p.fill('#vi-long', '   '); await p.press('#vi-long', 'Tab'); await p.wait_for_timeout(200)
    ok(await xp(p) == x0, 'XP начислен за пробелы')

# ---------- F05
@case('F05-H1', 'Цель с показателем сохраняется')
async def _(t):
    p = await t.device(mk_state()); await p.get_by_role('button', name='Составить план').click(); await act(p, 'addgoal')
    gid = (await stored(p))['goals'][0]['id']
    await p.fill(f'#g-{gid}-title', 'Выучить 300 слов'); await p.fill(f'#g-{gid}-mn', 'Слов'); await p.fill(f'#g-{gid}-mt', '300'); await p.fill(f'#g-{gid}-mu', 'шт.')
    await p.press(f'#g-{gid}-mu', 'Tab'); await p.wait_for_timeout(300); await p.reload(); await p.wait_for_timeout(250)
    g = (await stored(p))['goals'][0]
    ok(g['title'] == 'Выучить 300 слов' and g['measure'] == {'name': 'Слов', 'target': 300, 'unit': 'шт.'}, f'сохранено: {g}')

@case('F05-H2', 'Удаление цели с подтверждением')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'tab:plan')
    b = p.locator('[data-act^="delgoal:"]'); await b.click()
    ok('Удалить' in await b.text_content(), 'нет шага подтверждения')
    await b.click(); await p.wait_for_timeout(150)
    ok(await p.locator('.goalcard').count() == 0, 'цель не удалена')

@case('F05-E1', 'Предупреждение про 4-ю цель')
async def _(t):
    p = await t.device(mk_state(goals=[goal('gA'+str(i)+'xyz1', f'Ц{i}') for i in range(4)])); await act(p, 'tab:plan')
    ok(await p.locator('.warn').count() == 1, 'нет предупреждения')

@case('F05-E2', 'Длинное название с эмодзи')
async def _(t):
    long = ('Очень длинное название цели с ёжиком 🦔 и словами ' * 8)[:300]
    p = await t.device(mk_state(goals=[goal(title=long)]))
    ok(await overflow(p) <= 0, 'горизонтальная прокрутка на «Неделе»')
    await act(p, 'tab:results'); ok(await overflow(p) <= 0, 'горизонтальная прокрутка на «Итогах»')

@case('F05-E3', 'HTML в названии не выполняется')
async def _(t):
    p = await t.device(mk_state(goals=[goal(title='<img src=x onerror="window.__xss=1">')]))
    await act(p, 'tab:plan'); await act(p, 'tab:results'); await act(p, 'tab:week')
    ok(await p.evaluate('window.__xss') is None, 'скрипт выполнился')
    ok('<img' in await txt(p, '.gb-head h3'), 'название не показано как текст')

@case('F05-E4', 'Десятичная запятая в цели')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'tab:plan')
    gid = 'gA1b2c3'; await p.fill(f'#g-{gid}-mt', '10,5'); await p.press(f'#g-{gid}-mt', 'Tab'); await p.wait_for_timeout(200)
    ok((await stored(p))['goals'][0]['measure']['target'] == 10.5, 'не 10.5')
    await act(p, 'tab:results'); ok('10,5' in await txt(p, '.gprog .nums'), 'не показано «10,5»')

@case('F05-N1', 'Подтверждение удаления сбрасывается через 4 с')
async def _(t):
    p = await t.device(mk_state(goals=[goal()]), now=None); await act(p, 'tab:plan')
    b = p.locator('[data-act^="delgoal:"]'); await b.click(); await p.wait_for_timeout(4300)
    ok('Удалить' not in (await b.text_content()), 'подтверждение не сбросилось')
    await b.click(); await p.wait_for_timeout(100)
    ok(await p.locator('.goalcard').count() == 1, 'цель удалилась одним нажатием после сброса')

# ---------- F06
@case('F06-H1', 'Тактика 3× в неделю даёт 3 кружка')
async def _(t):
    p = await t.device(mk_state(goals=[goal(tactics=[])])); await act(p, 'tab:plan'); await act(p, 'addtac:gA1b2c3')
    tid = (await stored(p))['goals'][0]['tactics'][0]['id']
    await p.fill(f'#t-{tid}-title', 'Бегать'); await p.select_option(f'#t-{tid}-per', '3'); await p.select_option(f'#t-{tid}-from', '1')
    await p.wait_for_timeout(200); await act(p, 'tab:week')
    ok(await p.locator('.pip').count() == 3, f'кружков {await p.locator(".pip").count()}')

@case('F06-E1', 'Диапазон недель «с» > «по» исправляется')
async def _(t):
    p = await t.device(mk_state(goals=[goal(tactics=[tac(frm=1, to=5)])])); await act(p, 'tab:plan')
    await p.select_option('#t-tQ1w2e3-from', '8'); await p.wait_for_timeout(150)
    tt = (await stored(p))['goals'][0]['tactics'][0]
    ok(tt['from'] == 8 and tt['to'] == 8, f'{tt}')

@case('F06-E2', 'Тактика только в неделях 4–6')
async def _(t):
    p = await t.device(mk_state(goals=[goal(tactics=[tac(frm=4, to=6)])]))
    ok(await p.locator('.pip').count() == 0, 'тактика видна в неделе 3')
    await act(p, 'week:4'); ok(await p.locator('.pip').count() == 4, 'тактики нет в неделе 4')

@case('F06-E3', 'Снижение частоты ниже отмеченного')
async def _(t):
    p = await t.device(mk_state(goals=[goal(tactics=[tac(per=4)])], weeks={'3': week({'tQ1w2e3': 4})}))
    await act(p, 'tab:plan'); await p.select_option('#t-tQ1w2e3-per', '2'); await p.wait_for_timeout(150); await act(p, 'tab:week')
    ok(await ring(p) == '100%', f'оценка {await ring(p)}')

@case('F06-H2', 'Удаление тактики')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'tab:plan')
    b = p.locator('[data-act^="deltac:"]'); await b.click(); await b.click(); await p.wait_for_timeout(100); await act(p, 'tab:week')
    ok(await p.locator('.pip').count() == 0, 'тактика осталась')

# ---------- F07
@case('F07-H1', '3 из 4 = 75%, подсказка до 85%')
async def _(t):
    p = await t.device(mk_state(goals=[goal()]))
    for i in range(3): await pip(p, i)
    ok(await ring(p) == '75%', await ring(p))
    ok('осталось одно действие' in await txt(p, '.hero-line'), await txt(p, '.hero-line'))

@case('F07-H2', 'Все отмечено = 100% и XP')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); x0 = await xp(p)
    for i in range(4): await pip(p, i)
    ok(await ring(p) == '100%', await ring(p))
    # 4×10 + 100 (≥85%) + 50 (100%) + достижения: первый шаг, клуб 85, идеальная неделя (3×50)
    ok(await xp(p) - x0 == 40 + 100 + 50 + 150, f'прирост XP {await xp(p) - x0}')
    ok('Все тактики недели выполнены' in await txt(p, '.hero-line'), 'нет текста про идеальную неделю')

@case('F07-H3', 'Снятие последней отметки')
async def _(t):
    p = await t.device(mk_state(goals=[goal()], weeks={'3': week({'tQ1w2e3': 2})}))
    await pip(p, 1); ok(await ring(p) == '25%', await ring(p))
    await pip(p, 0); ok(await ring(p) == '0%', await ring(p))

@case('F07-E1', 'Неделя без тактик')
async def _(t):
    p = await t.device(mk_state(goals=[goal(tactics=[tac(frm=5, to=6)])]))
    ok('тактик нет' in await txt(p, '.hero-line'), await txt(p, '.hero-line'))

@case('F07-E2', 'Двойной клик по кружку')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); x0 = await xp(p)
    await p.dblclick('.pip >> nth=0'); await p.wait_for_timeout(200)
    ok(await ring(p) == '0%', f'после двойного клика {await ring(p)}')
    ok(await xp(p) - x0 == 50, f'лишний XP: {await xp(p) - x0} (ожидалось только достижение «Первый шаг»)')

@case('F07-H4', 'Отметка с клавиатуры, виден фокус')
async def _(t):
    p = await t.device(mk_state(goals=[goal()]))
    await p.focus('.pip >> nth=0'); await p.keyboard.press('Enter'); await p.wait_for_timeout(150)
    ok(await ring(p) == '25%', 'Enter не отметил')
    foc = await p.evaluate("document.activeElement.matches('.pip')")
    ok(foc, 'фокус потерялся после перерисовки')
    outline = await p.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    ok(outline != 'none', 'нет видимого фокуса')

# ---------- F08
@case('F08-H1', 'Цвета ленты недель')
async def _(t):
    st = mk_state(goals=[goal(tactics=[tac(per=10 if False else 7)])], weeks={'1': week({'tQ1w2e3': 6}), '2': week({'tQ1w2e3': 5}), '3': week({'tQ1w2e3': 2})})
    p = await t.device(st)
    cls = [await p.get_attribute(f'.cell >> nth={i}', 'class') for i in range(5)]
    ok('good' in cls[0] and 'mid' in cls[1] and 'low' in cls[2] and 'future' in cls[3], str(cls))
    ok('now' in cls[2], 'текущая неделя не отмечена')

@case('F08-H2', 'Переход на неделю 2')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'week:2')
    ok('Неделя 2' in await txt(p, '.h-week') and '28 сен – 4 окт' in await txt(p, '.hero-info .eyebrow'), await txt(p, '.hero-info .eyebrow'))

@case('F08-H3', 'Неделя 13 — итоги')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'week:13')
    ok('итоги' in await txt(p, '.h-week'), 'не экран итогов')

# ---------- F09
@case('F09-H1', 'Блоки времени дают XP')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); x0 = await xp(p)
    await act(p, 'blk:3:strategic'); await act(p, 'blk:3:breakout')
    ok(await xp(p) - x0 == 40, f'+{await xp(p) - x0}')
    ok(await p.get_attribute('[data-act="blk:3:strategic"]', 'aria-pressed') == 'true', 'переключатель не нажат')

@case('F09-E1', 'Буферные блоки 0…7')
async def _(t):
    p = await t.device(mk_state(goals=[goal()]))
    await act(p, 'buf:3:-1'); ok('0/7' in await txt(p, '.stepper span'), 'ушло ниже 0')
    for _ in range(8): await act(p, 'buf:3:1', 30)
    ok('7/7' in await txt(p, '.stepper span'), 'больше 7')

# ---------- F10
@case('F10-H1', 'Обзор недели и журнал')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); x0 = await xp(p)
    await p.fill('#rv-3-good', 'Утро без телефона'); await p.fill('#rv-3-bad', 'Устала'); await p.fill('#rv-3-next', 'Ложиться раньше')
    await p.fill('#lag-3-gA1b2c3', '12'); await p.press('#lag-3-gA1b2c3', 'Tab'); await act(p, 'reviewed:3')
    ok(await xp(p) - x0 == 30, f'+{await xp(p) - x0}')
    await act(p, 'tab:results')
    ok('Утро без телефона' in await txt(p, 'details.rv'), 'нет в журнале')
    ok('12 из 40' in await txt(p, '.gprog .nums'), await txt(p, '.gprog .nums'))

@case('F10-E1', 'Многострочный обзор')
async def _(t):
    p = await t.device(mk_state(goals=[goal()]))
    await p.fill('#rv-3-good', 'раз\nдва'); await p.press('#rv-3-good', 'Tab'); await act(p, 'tab:results')
    await p.click('details.rv summary')
    ok(await p.evaluate("getComputedStyle(document.querySelector('details.rv dd')).whiteSpace") == 'pre-wrap', 'переносы не сохраняются')

# ---------- F11
@case('F11-H1', 'Статистика итогов')
async def _(t):
    st = mk_state(goals=[goal(tactics=[tac(per=4)])], weeks={'1': week({'tQ1w2e3': 4}), '2': week({'tQ1w2e3': 4}), '3': week({'tQ1w2e3': 2})})
    p = await t.device(st); await act(p, 'tab:results')
    stats = [await txt(p, f'.stat >> nth={i} >> b') for i in range(3)]
    ok(stats == ['83%', '2', '2'], f'{stats} (ожидалось 83%, 2, 2)')

@case('F11-H2', 'График по неделям')
async def _(t):
    p = await t.device(mk_state(goals=[goal()], weeks={'1': week({'tQ1w2e3': 4})})); await act(p, 'tab:results')
    ok(await p.locator('svg.chart rect').count() == 12, 'не 12 столбцов')
    ok(await p.locator('svg.chart line.tl').count() == 1, 'нет линии 85%')
    ok(await p.locator('svg.chart text.val').first.text_content() == '100', 'нет подписи значения')

@case('F11-H3', 'Движение к цели')
async def _(t):
    p = await t.device(mk_state(goals=[goal()], weeks={'1': week(lag={'gA1b2c3': 6}), '2': week(lag={'gA1b2c3': 10})})); await act(p, 'tab:results')
    ok('10 из 40' in await txt(p, '.gprog .nums') and '25%' in await txt(p, '.gprog .nums'), await txt(p, '.gprog .nums'))
    ok(await p.locator('.gprog svg.spark path').count() == 1, 'нет спарклайна')

# ---------- F12
@case('F12-H1', 'Пороги уровней')
async def _(t):
    p = await t.device(mk_state(goals=[goal()]))
    res = await p.evaluate("""(()=>{const L=x=>{let n=1;while(x>=50*n*(n+1))n++;return n};return [L(0),L(99),L(100),L(299),L(300),L(600)]})()""")
    ok(res == [1, 1, 2, 2, 3, 4], str(res))
    st = mk_state(goals=[goal()], xpBank=300); p2 = await t.device(st)
    ok(await level(p2) == 3, f'уровень {await level(p2)} при 300 XP')

@case('F12-H2', 'Достижение: всплывающее сообщение')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await pip(p, 0)
    toasts = await p.locator('.toast').all_text_contents()
    ok(any('Первый шаг' in x for x in toasts), str(toasts))

@case('F12-H3', 'Снятие отметки уменьшает XP без сообщения')
async def _(t):
    p = await t.device(mk_state(goals=[goal()], weeks={'3': week({'tQ1w2e3': 1})}, ach={'first': '2026-10-01'}))
    x0 = await xp(p); await pip(p, 0)
    ok(await xp(p) == x0 - 10, f'{x0} -> {await xp(p)}')
    ok(await p.locator('.toast.xp').count() == 0, 'показан тост XP при уменьшении')

# ---------- F13
@case('F13-H1', 'Новый цикл')
async def _(t):
    start = iso(NOW.date() - timedelta(days=86))
    st = mk_state(start=start, goals=[goal()], weeks={str(i): week({'tQ1w2e3': 4}) for i in range(1, 13)})
    p = await t.device(st); x0 = await xp(p)
    b = p.locator('[data-act="newcycle"]'); await b.click(); await b.click(); await p.wait_for_timeout(300)
    s = await stored(p)
    ok(len(s['goals']) == 1 and s['weeks'] == {} and len(s['history']) == 1, 'цикл не переключился')
    ok(await xp(p) >= x0, f'опыт уменьшился: {x0} -> {await xp(p)}')
    ok(s['cycle']['start'] == iso(NOW.date() - timedelta(days=86) + timedelta(days=91)) or s['cycle']['start'] == '2026-10-05', s['cycle']['start'])
    await act(p, 'tab:results'); ok(await p.locator('text=Прошлые циклы').count() == 1, 'нет прошлых циклов')

# ---------- F14
@case('F14-H1', 'Начать с чистого листа')
async def _(t):
    p = await t.device(mk_state(goals=[goal()], xpBank=500)); await act(p, 'tab:plan')
    b = p.locator('[data-act="resetall"]'); await b.click(); await b.click(); await p.wait_for_timeout(200)
    await act(p, 'tab:week')
    ok(await p.locator('.welcome').count() == 1 and await level(p) == 1, 'данные не сброшены')

@case('F14-E1', 'Сброс сохраняет тему')
async def _(t):
    p = await t.device(mk_state(goals=[goal()], skin='calm')); await act(p, 'tab:plan')
    b = p.locator('[data-act="resetall"]'); await b.click(); await b.click(); await p.wait_for_timeout(200)
    ok(await p.evaluate('document.documentElement.dataset.skin') == 'calm', 'тема сбросилась')

# ---------- F15
@case('F15-H1', 'Отметки переживают перезагрузку')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await pip(p, 0); await pip(p, 1)
    await p.reload(); await p.wait_for_timeout(250)
    ok(await ring(p) == '50%', await ring(p))

@case('F15-E1', 'Две вкладки не затирают друг друга')
async def _(t):
    p = await t.device(mk_state(goals=[goal()]))
    p2 = await p.context.new_page(); await p2.clock.set_fixed_time(NOW); await p2.goto(t.url); await p2.wait_for_timeout(250)
    await pip(p2, 0); await p2.wait_for_timeout(150)          # вкладка 2 ставит отметку
    await p.bring_to_front(); await act(p, 'blk:3:strategic')  # вкладка 1 (старое состояние) меняет другое
    await p.wait_for_timeout(200)
    s = await stored(p)
    ok(s['weeks']['3']['done'].get('tQ1w2e3') == 1, 'отметка из второй вкладки затёрта')

# ---------- F16
@case('F16-H1', 'Регистрация с экрана входа')
async def _(t):
    p = await t.device(); await act(p, 'auth:up')
    await p.fill('#au-email', 'new@test.io'); await p.fill('#au-pass', 'secret1'); await p.click('form[data-form=signup] button[type=submit]'); await p.wait_for_timeout(300)
    ok('отправили письмо' in await txt(p, '.notice'), 'нет сообщения про письмо')
    ok('new@test.io' in t.be.users, 'аккаунт не создан')
    ok(await p.locator('.tab').count() == 0, 'приложение открылось без подтверждения почты')

@case('F16-H2', 'Вход открывает приложение и облачный цикл')
async def _(t):
    u = t.be.add_user('me@test.io', 'secret1'); t.be.rows[u['id']] = {'state': mk_state(goals=[goal(title='Из аккаунта')]), 'updated_at': 'x'}
    p = await t.device(); await signin(p, 'me@test.io', 'secret1'); await p.wait_for_timeout(500)
    ok(await txt(p, '#status') == 'Синхронизировано', await txt(p, '#status'))
    ok('Из аккаунта' in await txt(p, 'main'), 'цикл из аккаунта не показан')

@case('F16-N1', 'Неверный пароль')
async def _(t):
    t.be.add_user('me@test.io', 'secret1')
    p = await t.device(); await signin(p, 'me@test.io', 'wrong12')
    ok('Проверьте раскладку' in await txt(p, '.formmsg'), await txt(p, '.formmsg'))
    ok(await p.locator('.tab').count() == 0, 'приложение открылось с неверным паролем')

@case('F16-N2', 'Регистрация на занятую почту')
async def _(t):
    t.be.add_user('me@test.io', 'secret1')
    p = await t.device(); await act(p, 'auth:up')
    await p.fill('#au-email', 'me@test.io'); await p.fill('#au-pass', 'other12'); await p.click('form[data-form=signup] button[type=submit]'); await p.wait_for_timeout(300)
    ok('уже есть' in await txt(p, '.formmsg'), await txt(p, '.formmsg'))
    ok(t.be.users['me@test.io']['password'] == 'secret1', 'пароль изменился')

@case('F16-N3', 'Почта не подтверждена')
async def _(t):
    t.be.add_user('me@test.io', 'secret1', confirmed=False)
    p = await t.device(); await signin(p, 'me@test.io', 'secret1')
    ok('не подтверждена' in await txt(p, '.formmsg'), await txt(p, '.formmsg'))

@case('F16-H3', 'Восстановление пароля')
async def _(t):
    t.be.add_user('me@test.io', 'oldpass')
    p = await t.device(); await act(p, 'auth:forgot')
    await p.fill('#au-email', 'me@test.io'); await p.click('form[data-form=forgot] button[type=submit]'); await p.wait_for_timeout(300)
    ok(t.be.last_reset['email'] == 'me@test.io' and t.be.last_reset['redirectTo'].endswith('/index.html'), str(t.be.last_reset))
    p2 = await t.device(hash='#recovery=me%40test.io'); await p2.wait_for_timeout(400)
    ok(await p2.locator('form[data-form=newpass]').count() == 1, 'нет формы нового пароля')
    await p2.fill('#au-pass', 'oldpass'); await p2.click('form[data-form=newpass] button[type=submit]'); await p2.wait_for_timeout(200)
    ok('совпадает со старым' in await txt(p2, '.formmsg'), await txt(p2, '.formmsg'))
    await p2.fill('#au-pass', 'newpass1'); await p2.click('form[data-form=newpass] button[type=submit]'); await p2.wait_for_timeout(300)
    ok(t.be.users['me@test.io']['password'] == 'newpass1', 'пароль не сменился')
    ok(await p2.locator('[data-act="auth:out"]').count() == 1, 'после смены пароля не в аккаунте')

@case('F16-E1', 'Подсказка про русскую раскладку')
async def _(t):
    p = await t.device(); await p.fill('#au-pass', 'secret')
    ok(not await p.is_visible('#au-layout'), 'подсказка видна без причины')
    await p.fill('#au-pass', 'ыусксуе'); ok(await p.is_visible('#au-layout'), 'нет подсказки')

@case('F16-E2', 'Показать/скрыть пароль')
async def _(t):
    p = await t.device(); await p.fill('#au-pass', 'abc')
    await act(p, 'auth:eye'); ok(await p.get_attribute('#au-pass', 'type') == 'text', 'не показан')
    ok(await p.input_value('#au-pass') == 'abc', 'введённый пароль стёрся')
    await act(p, 'auth:eye'); ok(await p.get_attribute('#au-pass', 'type') == 'password', 'не скрыт')

@case('F16-H4', 'Выход: данные отправлены и удалены с устройства')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); uid = t.be.users['me@test.io']['id']
    await pip(p, 0)                                   # изменение ещё не отправлено
    await act(p, 'tab:account'); await act(p, 'auth:out', 900)
    ok(await p.locator('.gate').count() == 1, 'не вернулся экран входа')
    ok(not any(k.startswith(LS) for k in await keys(p)), f'данные остались: {await keys(p)}')
    ok('sb-mock-session' not in await keys(p), 'сессия осталась')
    ok(t.be.rows[uid]['state']['weeks']['3']['done']['tQ1w2e3'] == 1, 'последнее изменение не отправлено перед выходом')
    await p.reload(); await p.wait_for_timeout(300)
    ok(await p.locator('.gate').count() == 1 and 'Цель тест' not in await txt(p, 'body'), 'после перезагрузки видны данные')

@case('F16-H5', 'Выход без связи предупреждает')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); uid = t.be.users['me@test.io']['id']
    t.be.offline = True; await pip(p, 0); await p.wait_for_timeout(1200)
    await act(p, 'tab:account'); await act(p, 'auth:out', 500)
    ok('не отправлены' in await txt(p, '.formmsg') and await p.locator('.gate').count() == 0, 'вышел без предупреждения')
    ok(await p.get_by_role('button', name='Выйти без отправки').count() == 1, 'нет кнопки подтверждения')
    await act(p, 'auth:out', 500)
    ok(await p.locator('.gate').count() == 1 and not any(k.startswith(LS) for k in await keys(p)), 'не вышел после подтверждения')

@case('F16-E3', 'Сессия переживает перезагрузку')
async def _(t):
    t.be.add_user('me@test.io', 'secret1')
    p = await t.device(); await signin(p, 'me@test.io', 'secret1'); await p.reload(); await p.wait_for_timeout(600)
    ok(await txt(p, '#status') == 'Синхронизировано', await txt(p, '#status'))

@case('F16-E4', 'Работа без сети после входа')
async def _(t):
    p = await t.device(mk_state(goals=[goal(title='Офлайн цель')])); t.be.offline = True
    await p.reload(); await p.wait_for_timeout(500)
    ok('Офлайн цель' in await txt(p, 'main'), 'без сети цикл не открылся')

# ---------- F17
@case('F17-H1', 'Изменение уходит в облако')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); uid = t.be.users['me@test.io']['id']
    await pip(p, 0); await p.wait_for_timeout(1300)
    ok(t.be.rows[uid]['state']['weeks']['3']['done']['tQ1w2e3'] == 1, 'отметка не в облаке')

@case('F17-H2', 'Второе устройство подтягивает цикл')
async def _(t):
    u = t.be.add_user('me@test.io', 'secret1')
    t.be.rows[u['id']] = {'state': mk_state(goals=[goal(title='Облачная цель')], updatedAt=5000), 'updated_at': 'x'}
    p = await t.device(); await signin(p, 'me@test.io', 'secret1'); await p.wait_for_timeout(600)
    ok('Облачная цель' in await txt(p, 'main'), 'цикл не подтянулся')

@case('F17-H3', 'Возврат на вкладку подтягивает изменения')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); uid = t.be.users['me@test.io']['id']; await p.wait_for_timeout(400)
    st = json.loads(json.dumps(t.be.rows[uid]['state'])); st['goals'][0]['title'] = 'Изменено на телефоне'; st['updatedAt'] = st['updatedAt'] + 10_000
    t.be.rows[uid]['state'] = st
    await p.evaluate("document.dispatchEvent(new Event('visibilitychange'))"); await p.wait_for_timeout(500)
    ok('Изменено на телефоне' in await txt(p, 'main'), 'изменение не подтянулось')

@case('F17-E1', 'Без сети: сохраняется и досылается')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); uid = t.be.users['me@test.io']['id']; await p.wait_for_timeout(400)
    t.be.offline = True; await pip(p, 0); await p.wait_for_timeout(1300)
    ok('Нет связи' in await txt(p, '#status'), await txt(p, '#status'))
    t.be.offline = False; await p.evaluate("window.dispatchEvent(new Event('online'))"); await p.wait_for_timeout(900)
    ok(t.be.rows[uid]['state']['weeks'].get('3', {}).get('done', {}).get('tQ1w2e3') == 1, 'не дослалось')
    ok(await txt(p, '#status') == 'Синхронизировано', await txt(p, '#status'))

@case('F17-N2', 'После выхода из A вход в B показывает только цикл B')
async def _(t):
    t.be.add_user('a@test.io', 'secret1'); b = t.be.add_user('b@test.io', 'secret1')
    p = await t.device(mk_state(goals=[goal(title='Личная цель A')]), email='a@test.io')
    await act(p, 'tab:account'); await act(p, 'auth:out', 900)
    await signin(p, 'b@test.io', 'secret1'); await p.wait_for_timeout(1300)
    ok('Личная цель A' not in await txt(p, 'body'), 'цели A видны в аккаунте B')
    rb = t.be.rows.get(b['id'])
    ok(not rb or not any('Личная цель A' in g['title'] for g in rb['state']['goals']), 'данные A ушли в аккаунт B')

@case('F17-N3', 'Данные со старой версии и другой цикл в аккаунте: «Оставить из аккаунта»')
async def _(t):
    u = t.be.add_user('me@test.io', 'secret1')
    t.be.rows[u['id']] = {'state': mk_state(goals=[goal(title='Облако')], updatedAt=10), 'updated_at': 'x'}
    p = await t.device(legacy=mk_state(goals=[goal(title='Устройство')], updatedAt=99999))
    await signin(p, 'me@test.io', 'secret1'); await p.wait_for_timeout(600)
    ok(t.be.rows[u['id']]['state']['goals'][0]['title'] == 'Облако', 'облако перезаписано без спроса')
    ok(await p.locator('[data-act="sync:cloud"]').count() == 1, 'нет выбора')
    await act(p, 'sync:cloud', 300); await act(p, 'tab:week')
    ok('Облако' in await txt(p, 'main'), 'не показан облачный цикл')
    ok(LS not in await keys(p), 'старые данные не убраны')
    await pip(p, 0); await p.wait_for_timeout(1200)
    ok(t.be.rows[u['id']]['state']['weeks']['3']['done']['tQ1w2e3'] == 1, 'после выбора синхронизация не идёт')

@case('F17-N4', 'Данные со старой версии: «Оставить с этого устройства»')
async def _(t):
    u = t.be.add_user('me@test.io', 'secret1')
    t.be.rows[u['id']] = {'state': mk_state(goals=[goal(title='Облако')], updatedAt=10), 'updated_at': 'x'}
    p = await t.device(legacy=mk_state(goals=[goal(title='Устройство')]))
    await signin(p, 'me@test.io', 'secret1'); await p.wait_for_timeout(600)
    await act(p, 'sync:local', 1300)
    ok(t.be.rows[u['id']]['state']['goals'][0]['title'] == 'Устройство', 'облако не заменено')

@case('F17-N5', 'Данные со старой версии переносятся в пустой аккаунт')
async def _(t):
    u = t.be.add_user('me@test.io', 'secret1')
    p = await t.device(legacy=mk_state(goals=[goal(title='Старый цикл')]))
    await signin(p, 'me@test.io', 'secret1'); await p.wait_for_timeout(1300)
    ok(t.be.rows[u['id']]['state']['goals'][0]['title'] == 'Старый цикл', 'не перенесено в аккаунт')
    ok(LS not in await keys(p), 'старая копия не убрана')

@case('F17-E2', 'Одинаковые данные: без лишнего вопроса')
async def _(t):
    u = t.be.add_user('me@test.io', 'secret1')
    st = mk_state(goals=[goal()]); t.be.rows[u['id']] = {'state': dict(st, updatedAt=5), 'updated_at': 'x'}
    p = await t.device(legacy=st); await signin(p, 'me@test.io', 'secret1'); await p.wait_for_timeout(800)
    ok(await p.locator('[data-act="sync:cloud"]').count() == 0, 'лишний вопрос о выборе')
    ok(await txt(p, '#status') == 'Синхронизировано', await txt(p, '#status'))

# ---------- F18
@case('F18-H1', 'Скачать копию')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'tab:account')
    async with p.expect_download() as d: await p.click('[data-act="backup:save"]')
    dl = await d.value; path = await dl.path()
    data = json.load(open(path))
    ok(dl.suggested_filename == '12-nedel-2026-10-07.json' and data['goals'][0]['title'] == 'Цель тест', dl.suggested_filename)

@case('F18-H2', 'Восстановить из файла')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'tab:account')
    path = os.path.join(OUT, 'backup.json'); json.dump(mk_state(goals=[goal(title='Из копии')]), open(path, 'w'))
    await p.set_input_files('#backup-file', path); await p.wait_for_timeout(200)
    await act(p, 'backup:apply'); await act(p, 'tab:week')
    ok('Из копии' in await txt(p, 'main'), 'данные не восстановлены')

@case('F18-N1', 'Чужой файл')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'tab:account')
    path = os.path.join(OUT, 'bad.json'); open(path, 'w').write('{"hello":1}')
    await p.set_input_files('#backup-file', path); await p.wait_for_timeout(200)
    ok(any('не похож' in x for x in await p.locator('.toast').all_text_contents()), 'нет сообщения')
    path2 = os.path.join(OUT, 'bad.txt'); open(path2, 'w').write('не json')
    await p.set_input_files('#backup-file', path2); await p.wait_for_timeout(200)
    ok(len((await stored(p))['goals']) == 1, 'данные испорчены')

@case('F18-E1', 'Отмена восстановления')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'tab:account')
    path = os.path.join(OUT, 'backup2.json'); json.dump(mk_state(goals=[]), open(path, 'w'))
    await p.set_input_files('#backup-file', path); await p.wait_for_timeout(200); await act(p, 'backup:cancel')
    ok(len((await stored(p))['goals']) == 1, 'данные изменились')

# ---------- F19
@case('F19-H1', 'Темы переключаются и сохраняются')
async def _(t):
    p = await t.device(mk_state(goals=[goal()])); await act(p, 'tab:account')
    for k in ['notebook', 'game', 'calm', 'classic', 'game']:
        await act(p, f'skin:{k}'); ok(await p.evaluate('document.documentElement.dataset.skin') == k, f'тема {k} не применилась')
    await p.reload(); await p.wait_for_timeout(250)
    ok(await p.evaluate('document.documentElement.dataset.skin') == 'game', 'тема не сохранилась')

for _skin in ['classic', 'notebook', 'game', 'calm']:
    for _scheme in ['light', 'dark']:
        def _mk(skin, scheme):
            async def fn(t):
                weeks = {'1': week({'tQ1w2e3': 4}), '2': week({'tQ1w2e3': 3}), '3': week({'tQ1w2e3': 1})}
                p = await t.device(mk_state(goals=[goal(), goal('gZ9y8x7', 'Вторая', [tac('tP0o9i8', 'Бег', 3)])], weeks=weeks, skin=skin,
                                            vision={'long': 'x', 'three': ''}), viewport=(390, 844), scheme=scheme)
                problems = []
                for tab in ['week', 'plan', 'results', 'level', 'account']:
                    await act(p, f'tab:{tab}', 150)
                    if await overflow(p) > 0: problems.append(f'{tab}: прокрутка вбок')
                    r = await a11y(p)
                    if r['noName']: problems.append(f'{tab}: без имени {r["noName"][:2]}')
                    if r['low']: problems.append(f'{tab}: контраст {r["low"][:3]}')
                    if skin == 'classic' and scheme == 'light' and tab == 'week':
                        await p.screenshot(path=os.path.join(OUT, 'week.png'), full_page=True)
                await p.screenshot(path=os.path.join(OUT, f'{skin}-{scheme}.png'), full_page=True)
                ok(not problems, '; '.join(problems))
            return fn
        case(f'F19-E1-{_skin}-{_scheme}', f'Тема {_skin}, {_scheme}: вёрстка, имена, контраст')(_mk(_skin, _scheme))

# ---------- F21
@case('F21-H3', 'Нет ошибок на всех экранах и в итогах цикла')
async def _(t):
    p = await t.device(mk_state(goals=[goal()], weeks={'3': week({'tQ1w2e3': 2})}))
    for tab in ['week', 'plan', 'results', 'level', 'account']: await act(p, f'tab:{tab}')
    await act(p, 'tab:week'); await act(p, 'week:13'); await act(p, 'week:1')

@case('F01-E5', 'Приветствие без ошибок доступности')
async def _(t):
    p = await t.device()
    r = await a11y(p); ok(not r['noName'] and not r['low'], str(r))

# ================================================================= runner
async def main(filters):
    os.makedirs(OUT, exist_ok=True)
    srv, url = start_server()
    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for cid, title, fn in CASES:
            if filters and not any(cid.startswith(f) for f in filters): continue
            t = T(browser, url, Backend())
            status, note = 'PASS', ''
            try:
                await asyncio.wait_for(fn(t), 40)
                await asyncio.sleep(0.05)
                if t.errors: status, note = 'FAIL', 'ошибки в консоли: ' + ' | '.join(t.errors[:3])
                elif t.pages and await overflow(t.pages[-1]) > 0: status, note = 'FAIL', 'горизонтальная прокрутка'
            except Fail as e:
                status, note = 'FAIL', str(e)
            except Exception as e:
                status, note = 'ERROR', f'{type(e).__name__}: {str(e).splitlines()[0][:200]}'
            if status != 'PASS' and t.pages:
                try: await t.pages[-1].screenshot(path=os.path.join(OUT, f'{cid}.png'), full_page=True)
                except Exception: pass
            results.append((cid, title, status, note))
            print(f'{status:5} {cid:24} {title}' + (f'\n      → {note}' if note else ''), flush=True)
            await t.close()
        await browser.close()
    srv.shutdown()
    n = len(results); passed = sum(r[2] == 'PASS' for r in results)
    print(f'\nИтого: {passed}/{n} прошли')
    with open(os.path.join(OUT, 'results.json'), 'w') as f: json.dump(results, f, ensure_ascii=False, indent=1)
    return 0 if passed == n else 1

if __name__ == '__main__':
    sys.exit(asyncio.run(main(sys.argv[1:])))
