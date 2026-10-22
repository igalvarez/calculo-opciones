import requests, re
url = ("https://finviz.com/screener/stocks?v=111&f=cap_smallover,targetprice_a50,"
       "an_recom_buybetter,ta_sma20_pb20,geo_usa&o=-marketcap&ft=4&r=1")
h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
     "Referer": "https://finviz.com/"}
r = requests.get(url, headers=h, timeout=20)
html = r.text
open("finviz_debug.html", "w", encoding="utf-8").write(html)
print("HTTP:", r.status_code, "| bytes:", len(html))
print("<tr>               :", html.count("<tr"))
print("quote.ashx?t=      :", len(re.findall(r'quote\.ashx\?t=', html)))
print("/quote?t=          :", len(re.findall(r'/quote\?t=', html)))
print('"ticker":          :', len(re.findall(r'"ticker"\s*:', html, re.I)))
print("RKLB aparece       :", html.count("RKLB"))
i = html.find("RKLB")
print("\n--- contexto RKLB ---\n", html[max(0, i-600): i+400] if i >= 0 else "NO")