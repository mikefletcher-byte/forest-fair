import base64, io, json, os, re
from PIL import Image

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# 1. ЧИСТИМ ПРОБЕЛЫ В BOT.PY
if os.path.exists("bot.py"):
    bot_code = open("bot.py", "r", encoding="utf-8").read().replace("\u00a0", " ")
    open("bot.py", "w", encoding="utf-8").write(bot_code)
    print("OK: bot.py очищен от кривых пробелов!")

# 2. СОЗДАЕМ SW.JS (ЕСЛИ ЕГО НЕТ)
sw_code = """self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (e) => e.respondWith(fetch(e.request).catch(() => caches.match(e.request))));
"""
open("sw.js", "w", encoding="utf-8").write(sw_code)

# 3. БЕРЕМ ЧИСТЫЙ ИСХОДНИК INDEX.HTM
html = open("index.htm", "r", encoding="utf-8").read().replace("\u00a0", " ")
open("index.htm", "w", encoding="utf-8").write(html)

# 4. СОЗДАЕМ PNG-ИКОНКИ И ВШИВАЕМ МАНИФЕСТ ПРЯМО В INDEX.HTML
logo_path = next((f for f in ["logo.jpg", "logo.jpeg", "logo.png"] if os.path.exists(f)), None)
if not logo_path and os.path.exists("Pictures"):
    logo_path = next((os.path.join("Pictures", f) for f in os.listdir("Pictures") if f.lower().startswith("red_cap")), None)

if logo_path:
    img_logo = Image.open(logo_path).convert("RGBA")
    
    # Сохраняем физические файлы иконок
    icon192 = img_logo.resize((192, 192), Image.Resampling.LANCZOS)
    icon512 = img_logo.resize((512, 512), Image.Resampling.LANCZOS)
    icon192.save("icon-192.png", "PNG", optimize=True)
    icon512.save("icon-512.png", "PNG", optimize=True)

    # Переводим иконку 192x192 в Base64, чтобы она 100% открывалась без ошибок 404
    buf192 = io.BytesIO()
    icon192.save(buf192, format="PNG", optimize=True)
    b64_icon192 = "data:image/png;base64," + base64.b64encode(buf192.getvalue()).decode("utf-8")

    # Формируем правильный manifest.json
    manifest_data = {
        "id": "/forest-fair/",
        "name": "Forest Fair — Лесная Ярмарка",
        "short_name": "Forest Fair",
        "description": "Сибирская таежная лавка дикоросов, отборных грибов и премиальных чаев",
        "start_url": "/forest-fair/",
        "scope": "/forest-fair/",
        "display": "standalone",
        "background_color": "#111916",
        "theme_color": "#111916",
        "icons": [
            {
                "src": "icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any"
            },
            {
                "src": "icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any"
            }
        ]
    }
    open("manifest.json", "w", encoding="utf-8").write(json.dumps(manifest_data, ensure_ascii=False, indent=2))

    # Заменяем иконку в шапке HTML на встроенную Base64, чтобы буква G исчезла навсегда!
    html = html.replace('href="icon-192.png"', 'href="' + b64_icon192 + '"')
    print("OK: Иконки icon-192.png, icon-512.png и manifest.json созданы и вшиты!")

# 5. СЖИМАЕМ И ВШИВАЕМ 16 КАРТИНОК ТОВАРОВ
all_root_files = os.listdir(".")
pic_dir = "Pictures" if os.path.exists("Pictures") else "."
pic_files = os.listdir(pic_dir)

product_images = {
    "red_cap": ["red_cap"],
    "red_cap_2": ["red_cap_2", "red_cap"],
    "panther_extra": ["panther_extra"],
    "panther_cap_2": ["panther_cap_2", "panther_extra"],
    "panther_1": ["panther_1", "panther_extra"],
    "panther_second": ["panther_second"],
    "chanterelle_1": ["chanterelle_1", "red_cap"],
    "chanterelle_2": ["chanterelle_2", "chanterelle_1", "red_cap"],
    "gaba_tea": ["gaba_tea"],
    "gaba_topaz": ["gaba_topaz", "gaba_tea"],
    "gaba_diamond": ["gaba_diamond", "gaba_tea"],
    "puer_tea": ["puer_tea"],
    "puer_xiatuo": ["puer_xiatuo", "puer_tea"],
    "lion_mane_1kg": ["lion_mane_1kg", "lion_mane"],
    "lion_mane": ["lion_mane"],
    "taiga_herbs": ["taiga_herbs"]
}

for prod_id, candidates in product_images.items():
    found_path = None
    for cand in candidates:
        match = next((f for f in pic_files if f.lower().startswith(cand + ".") and f.lower().endswith((".jpg", ".jpeg", ".png"))), None)
        if match:
            found_path = os.path.join(pic_dir, match)
            break
        match_root = next((f for f in all_root_files if f.lower().startswith(cand + ".") and f.lower().endswith((".jpg", ".jpeg", ".png"))), None)
        if match_root:
            found_path = match_root
            break

    if found_path:
        img = Image.open(found_path).convert("RGB")
        img.thumbnail((440, 440), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        new_data_uri = "data:image/jpeg;base64," + b64

        pattern = r'(id:\s*"' + prod_id + r'"[\s\S]*?img:\s*")[^"]*(")'
        html = re.sub(pattern, r'\g<1>' + new_data_uri + r'\g<2>', html)
        print("OK для", prod_id, "-> взят файл:", found_path)

open("index.html", "w", encoding="utf-8").write(html)
print("ГОТОВО! Все файлы для PWA и GitHub успешно собраны!")