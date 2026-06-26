from pyrogram import Client

# စောစောက my.telegram.org ကရလာတဲ့ id နှင့် hash ကို အောက်တွင် အစားထိုးပါ
api_id = 34530292  # ဤနေရာတွင် အစ်ကို၏ api_id ဂဏန်းကို ထည့်ပါ
api_hash = "c099e90c10625fb73da091b80e906816"

app = Client("my_account", api_id=api_id, api_hash=api_hash)

with app:
    print("\n👇 ဤသည်မှာ အစ်ကို၏ Session String ဖြစ်ပါသည် 👇\n")
    print(app.export_session_string())
    print("\n👆 အထက်ပါ စာတန်းရှည်ကြီးကို သေချာ Copy ကူးထားပါ 👆")

