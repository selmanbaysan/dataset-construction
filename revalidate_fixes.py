"""Re-validate the 4 rebuilt datasets' specific fixes (run on the instance; data in /build)."""
import re
from datasets import load_from_disk

B = "/build"


def load(p):
    return load_from_disk(p)


# sayistay: trailing site footer removed?
ds = load(f"{B}/sayistay/out/legal_sayistay")
foot = sum(1 for t in ds["text"] if "İletişim Bilgileri" in t or "Sayıştay Başkanlığı ©" in t)
print(f"[sayistay] rows={ds.num_rows} footer_docs={foot} (expect 0)")
print(f"           tail={ds[0]['text'][-80:]!r}")

# wikibooks: edit-link / template cruft removed?
ds = load(f"{B}/wikibooks/out/edu_wikibooks_tr")
edit = sum(1 for t in ds["text"] if re.search(r"\[\s*düzenle\s*\]", t))
tmpl = sum(1 for t in ds["text"] if "{{" in t)
print(f"[wikibooks] rows={ds.num_rows} duzenle_docs={edit} template_docs={tmpl} (expect ~0)")

# anayasa: only bireysel, no generic titles, no modal chrome, bounded length?
ds = load(f"{B}/anayasa/out/legal_anayasa_bireysel")
generic = sum(1 for t in ds["title"] if t.strip() == "AYM Bireysel Başvuru Kararı")
kapat = sum(1 for t in ds["text"][:5000] if "\nKapat\n" in t)
mx = max(len(t) for t in ds["text"])
print(f"[anayasa] rows={ds.num_rows} generic_titles={generic} (expect 0) "
      f"kapat_in_first5k={kapat} max_len={mx}")
print(f"          title_sample={ds[3]['title']!r}")

# econbiz: source-label prefix removed?
ds = load(f"{B}/econbiz/out/acad_econbiz_abstracts")
pfx = sum(1 for t in ds["text"] if t.lower().startswith(("turkish abstract", "english abstract", "abstract:")))
print(f"[econbiz] rows={ds.num_rows} prefix_docs={pfx} (expect 0)")
