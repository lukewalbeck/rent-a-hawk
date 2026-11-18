import re, json, requests
H={"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36","Accept":"text/html,application/xhtml+xml","Accept-Language":"en-US,en;q=0.9"}
s=requests.Session()
for u in ["https://www.zumper.com/apartments-for-rent/san-francisco-ca/1,2-beds/under-5800?listing-amenities=16",
          "https://www.padmapper.com/apartments/san-francisco-ca?box=-122.53,37.70,-122.35,37.83&bedrooms=1,2&max-price=5800"]:
    try:
        r=s.get(u,headers=H,timeout=25)
        t=r.text
        print("URL",u,"status",r.status_code,"len",len(t))
        print("BODY",t[:1500].replace("\n"," "))
        print(" cf-challenge" if "cf-chl" in t or "Just a moment" in t else " no-cf-challenge")
        for k in ["__NEXT_DATA__","__PRELOADED_STATE__","window.__","listables","\"lat\"","min_price","\"address\"","application/ld+json"]:
            print("  ",k,t.count(k))
        m=re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',t,re.S)
        if m:
            d=json.loads(m.group(1)); print("  next keys",list(d.get("props",{}).get("pageProps",{}).keys())[:20])
        lds=re.findall(r'<script type="application/ld\+json">(.*?)</script>',t,re.S)
        for ld in lds[:3]: print("  LD",ld[:300].replace("\n"," "))
        i=t.find("listables"); print("  ctx",t[max(0,i-200):i+400].replace("\n"," ") if i>0 else "")
    except Exception as e: print("ERR",u,e)
