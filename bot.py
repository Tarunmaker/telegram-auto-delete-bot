import asyncio, json, logging, os, re, sqlite3, time, urllib.request
from pathlib import Path
from telegram import Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
DB_PATH = os.getenv("DATABASE_PATH", "/tmp/xoxoz.sqlite3")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("xoxoz")

class Store:
    def __init__(self):
        self.redis = bool(REDIS_URL and REDIS_TOKEN)
        if self.redis: return
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS settings(chat INTEGER PRIMARY KEY, delay INTEGER DEFAULT 0, filter_on INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS jobs(chat INTEGER, msg INTEGER, due REAL, PRIMARY KEY(chat,msg));
        CREATE TABLE IF NOT EXISTS words(chat INTEGER, word TEXT, PRIMARY KEY(chat,word));
        CREATE TABLE IF NOT EXISTS allowed(chat INTEGER, uid INTEGER, PRIMARY KEY(chat,uid));
        """)
        self.db.commit()
    def r(self, *args):
        req=urllib.request.Request(REDIS_URL,data=json.dumps(list(args)).encode(),
          headers={"Authorization":f"Bearer {REDIS_TOKEN}","Content-Type":"application/json"},method="POST")
        with urllib.request.urlopen(req,timeout=10) as x: data=json.loads(x.read())
        if data.get("error"): raise RuntimeError(data["error"])
        return data.get("result")
    def setting(self,c,k,default=0):
        if self.redis:
            v=self.r("GET",f"xz:{k}:{c}"); return int(v) if v is not None else default
        row=self.db.execute(f"SELECT {k} FROM settings WHERE chat=?",(c,)).fetchone()
        return int(row[k]) if row else default
    def set(self,c,k,v):
        if self.redis: self.r("SET",f"xz:{k}:{c}",v); return
        self.db.execute("INSERT OR IGNORE INTO settings(chat) VALUES(?)",(c,))
        self.db.execute(f"UPDATE settings SET {k}=? WHERE chat=?",(v,c)); self.db.commit()
    def schedule(self,c,m,delay):
        due=time.time()+delay
        if self.redis: self.r("ZADD","xz:jobs",due,f"{c}:{m}")
        else: self.db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?)",(c,m,due)); self.db.commit()
    def due(self):
        if self.redis:
            vals=self.r("ZRANGEBYSCORE","xz:jobs","-inf",time.time()) or []
            out=[]
            for v in vals[:100]:
                try: c,m=v.split(":"); out.append((int(c),int(m)))
                except: self.r("ZREM","xz:jobs",v)
            return out
        return [(r["chat"],r["msg"]) for r in self.db.execute("SELECT chat,msg FROM jobs WHERE due<=? LIMIT 100",(time.time(),)).fetchall()]
    def job_done(self,c,m,retry=False):
        if self.redis:
            if retry: self.r("ZADD","xz:jobs",time.time()+15,f"{c}:{m}")
            else: self.r("ZREM","xz:jobs",f"{c}:{m}")
        elif retry:
            self.db.execute("UPDATE jobs SET due=? WHERE chat=? AND msg=?",(time.time()+15,c,m)); self.db.commit()
        else:
            self.db.execute("DELETE FROM jobs WHERE chat=? AND msg=?",(c,m)); self.db.commit()
    def words(self,c):
        if self.redis: return self.r("SMEMBERS",f"xz:words:{c}") or []
        return [r[0] for r in self.db.execute("SELECT word FROM words WHERE chat=?",(c,)).fetchall()]
    def word(self,c,w,add):
        w=w.casefold().strip()
        if self.redis: self.r("SADD" if add else "SREM",f"xz:words:{c}",w)
        elif add:
            self.db.execute("INSERT OR IGNORE INTO words VALUES(?,?)",(c,w)); self.db.commit()
        else:
            self.db.execute("DELETE FROM words WHERE chat=? AND word=?",(c,w)); self.db.commit()
    def allow(self,c,u,add):
        if self.redis: self.r("SADD" if add else "SREM",f"xz:allowed:{c}",u)
        elif add:
            self.db.execute("INSERT OR IGNORE INTO allowed VALUES(?,?)",(c,u)); self.db.commit()
        else:
            self.db.execute("DELETE FROM allowed WHERE chat=? AND uid=?",(c,u)); self.db.commit()
    def is_allowed(self,c,u):
        if self.redis: return bool(self.r("SISMEMBER",f"xz:allowed:{c}",u))
        return self.db.execute("SELECT 1 FROM allowed WHERE chat=? AND uid=?",(c,u)).fetchone() is not None

s=Store()

async def admin(update,ctx):
    ch,u=update.effective_chat,update.effective_user
    if not ch or ch.type not in ("group","supergroup") or not u: return False
    try: return (await ctx.bot.get_chat_member(ch.id,u.id)).status in ("administrator","creator")
    except TelegramError: return False

async def reply(update,ctx,text):
    m=update.effective_message
    if m:
        try:
            sent=await m.reply_text(text); s.schedule(sent.chat_id,sent.message_id,20)
        except TelegramError as e: log.warning("reply failed: %s",e)

async def guard(update,ctx):
    if not update.effective_chat or update.effective_chat.type not in ("group","supergroup"):
        await reply(update,ctx,"Use this command in a group."); return False
    if not await admin(update,ctx):
        await reply(update,ctx,"Only group admins can change settings."); return False
    return True

async def start(u,c): await reply(u,c,"XOXOZ AutoDelete ready. Promote me with Delete Messages + Ban Users permissions. /help")
async def helpcmd(u,c):
    await reply(u,c,"Admin commands:\\n/settime 2 — delete after 2 minutes\\n/off /time\\n/addword phrase\\n/delword phrase\\n/words\\n/banword on|off\\n/allowuser USER_ID\\n/unallowuser USER_ID\\nMatching non-admin, non-allowed members are permanently banned when the filter is ON.")
async def settime(u,c):
    if not await guard(u,c): return
    if len(c.args)!=1 or not c.args[0].isdigit() or not 1<=int(c.args[0])<=1440:
        await reply(u,c,"Usage: /settime 1 (minutes, 1–1440)"); return
    s.set(u.effective_chat.id,"delay",int(c.args[0])*60); await reply(u,c,f"Auto-delete set to {c.args[0]} minute(s).")
async def off(u,c):
    if await guard(u,c): s.set(u.effective_chat.id,"delay",0); await reply(u,c,"Auto-delete OFF.")
async def showtime(u,c):
    ch=u.effective_chat
    if not ch or ch.type not in ("group","supergroup"): await reply(u,c,"Use in a group."); return
    d=s.setting(ch.id,"delay"); await reply(u,c,"Auto-delete OFF." if not d else f"Timer: {d//60} minute(s).")
async def addword(u,c):
    if not await guard(u,c): return
    w=" ".join(c.args).strip()
    if not w or len(w)>80: await reply(u,c,"Usage: /addword word or phrase (max 80 chars)"); return
    s.word(u.effective_chat.id,w,True); await reply(u,c,f"Added: {w}")
async def delword(u,c):
    if not await guard(u,c): return
    w=" ".join(c.args).strip()
    if not w: await reply(u,c,"Usage: /delword word or phrase"); return
    s.word(u.effective_chat.id,w,False); await reply(u,c,f"Removed: {w}")
async def words(u,c):
    if not await guard(u,c): return
    w=s.words(u.effective_chat.id); await reply(u,c,"Banned words/phrases:\\n"+("\\n".join("• "+x for x in w) if w else "None"))
async def banword(u,c):
    if not await guard(u,c): return
    if len(c.args)!=1 or c.args[0].lower() not in ("on","off"):
        await reply(u,c,"Usage: /banword on OR /banword off"); return
    s.set(u.effective_chat.id,"filter_on",int(c.args[0].lower()=="on"))
    await reply(u,c,"Ban-word filter "+("ON" if c.args[0].lower()=="on" else "OFF"))
async def allowuser(u,c,add):
    if not await guard(u,c): return
    if len(c.args)!=1 or not c.args[0].lstrip("-").isdigit():
        await reply(u,c,"Usage: /allowuser USER_ID"); return
    uid=int(c.args[0]); s.allow(u.effective_chat.id,uid,add)
    await reply(u,c,f"User {uid} "+("exempted." if add else "exemption removed."))
async def allowcmd(u,c): await allowuser(u,c,True)
async def unallowcmd(u,c): await allowuser(u,c,False)

def match_word(text,words):
    t=text.casefold()
    for w in words:
        w=w.casefold().strip()
        if not w: continue
        pattern=re.escape(w) if " " in w else rf"(?<!\\w){re.escape(w)}(?!\\w)"
        if re.search(pattern,t,re.UNICODE): return w
    return None

async def on_message(u,c):
    m,ch,user=u.effective_message,u.effective_chat,u.effective_user
    if not m or not ch or ch.type not in ("group","supergroup") or not user or user.is_bot: return
    delay=s.setting(ch.id,"delay")
    if delay: s.schedule(ch.id,m.message_id,delay)
    if not s.setting(ch.id,"filter_on") or s.is_allowed(ch.id,user.id): return
    if m.entities and any(e.type=="bot_command" for e in m.entities): return
    try:
        cm=await c.bot.get_chat_member(ch.id,user.id)
        if cm.status in ("administrator","creator"): return
    except TelegramError:
        log.exception("Cannot check member status"); return
    text=m.text or m.caption or ""
    found=match_word(text,s.words(ch.id))
    if not found: return
    try: await c.bot.delete_message(ch.id,m.message_id)
    except TelegramError: log.exception("Matched message deletion failed")
    try:
        await c.bot.ban_chat_member(ch.id,user.id)  # no until_date = permanent ban
        log.info("Permanently banned %s for configured phrase",user.id)
        await c.bot.send_message(ch.id,"A member was permanently banned for using a prohibited word/phrase.")
    except (BadRequest,Forbidden) as e:
        log.warning("Ban failed: %s",e)
        await c.bot.send_message(ch.id,"A prohibited phrase was detected, but the ban failed. Check the bot's Ban Users permission.")
    except TelegramError: log.exception("Ban API error")

async def worker(app):
    log.info("Deletion scheduler started")
    while True:
        try:
            for chat_id,msg_id in s.due():
                try:
                    await app.bot.delete_message(chat_id,msg_id); s.job_done(chat_id,msg_id)
                except (BadRequest,Forbidden) as e:
                    log.info("Delete skipped: %s",e); s.job_done(chat_id,msg_id)
                except TelegramError:
                    log.exception("Delete retry"); s.job_done(chat_id,msg_id,True)
        except Exception: log.exception("Scheduler error")
        await asyncio.sleep(2)
async def post_init(app): app.bot_data["worker"]=asyncio.create_task(worker(app))
async def post_shutdown(app):
    task=app.bot_data.get("worker")
    if task:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass

def main():
    if not TOKEN: raise RuntimeError("Set BOT_TOKEN in Render Environment")
    if not PUBLIC_URL: raise RuntimeError("RENDER_EXTERNAL_URL is missing")
    app=(Application.builder().token(TOKEN).post_init(post_init).post_shutdown(post_shutdown).build())
    for name,fn in [("start",start),("help",helpcmd),("settime",settime),("off",off),("time",showtime),
                    ("addword",addword),("delword",delword),("words",words),("banword",banword),
                    ("allowuser",allowcmd),("unallowuser",unallowcmd)]:
        app.add_handler(CommandHandler(name,fn))
    app.add_handler(MessageHandler(filters.ALL,on_message),group=1)
    log.info("Starting XOXOZ webhook at %s",PUBLIC_URL)
    app.run_webhook(listen="0.0.0.0",port=PORT,url_path="telegram",
      webhook_url=f"{PUBLIC_URL}/telegram",allowed_updates=Update.ALL_TYPES,
      drop_pending_updates=False,bootstrap_retries=5)

if __name__=="__main__": main()
