# GDL šablóny fasády (ADR-001 D5, inkrement I4)

Parametrické GDL objekty bez C++: XML šablóna → `LP_XMLConverter` (súčasť AC29) → `.gsm` → embedded library projektu.
Generátor: `facade/gdl_build.py`, test: `private-tests/test_i4_gdl.py`.

## Postup

```
python facade/gdl_build.py                 # kompilácia všetkých šablón (bez ArchiCADu), kontrola GDL
python facade/gdl_build.py --deploy        # + nahratie zmenených do embedded library otvoreného projektu
python facade/gdl_build.py FS_Louvers --deploy --force
```

1. `LP_XMLConverter convertlibrary -l UTF8 -reportlevel 2 -interpret -checkinit <src> <bin>` – skompiluje XML a interpretuje
   skripty; každá chyba GDL (okrem „Missing ancestor" – vstavané podtypy *General GDL Object* / *Model Element* nie sú súbory)
   = zlyhanie buildu. Timeout 120 s na volanie.
2. Kontrola `libpart2xml` späť: GUID a zoznam parametrov sa musia zhodovať so šablónou.
3. `.gsm` → `C:\FacadePilot\out\_gdl\<meno>.gsm` → `AddFilesToEmbeddedLibrary` (`FS_GDL\<meno>.gsm`, `type: Object`,
   `overwriteExisting: true`) → `ReloadLibraries` → `GetAvailableLibraryParts` (musí ho vidieť ako `Object`).
4. Idempotencia: `out/_gdl/manifest.json` drží hash (šablóna + prepísané predvolené hodnoty + build convertera); nezmenená
   šablóna, ktorá je v projekte načítaná, sa nekompiluje ani nenahráva.

Overené 2026-10-19: `LP_XMLConverter 29.0.0, build 3000, WIN64`, XML formát `Symbol Version="46"`, `ParamSection SectVersion="27"`;
round trip `libpart2xml → xml2libpart → libpart2xml` knižničnej `.gsm` je bezstratový (XML zhodné bajt po bajte; `.gsm` sa
binárne líši – časová pečiatka/kompresia).

## Konvencia súradníc (všetky šablóny)

Jednotky m, uhly v stupňoch. `x` = pozdĺž fasády (šírka), `z` = hore, `y` = **von z roviny steny** (vyloženie / odsadenie).
Objekt leží v rovine x–z, rovina steny je `y = 0`.

Umiestnenie na fasádu s osou `a → b`: počiatok v ľavom dolnom rohu poľa / otvoru a uhol natočenia `atan2(d)` , ak je exteriér
**vľavo** od `a → b`; ak je exteriér **vpravo** (`outside: right_of_ab`, napr. M015), uhol `atan2(d) + 180°` a počiatok na
**pravom** okraji (lokálne `x` potom beží proti `s`). Overené v teste K5 (M015, uhol 141°).

Spoločné: 2D symbol `PROJECT2 3, 270, 2` + `HOTSPOT2` na rohoch; 3D z `BLOCK` (každý prvok = 1 hranol, 8 vrcholov) + `HOTSPOT`
na rohoch obalového kvádra; povrchy ako parametre typu Material (`0` = povrch objektu z nastavení prvku). `A`, `ZZYZX` sú
rozmery poľa (FS_Surround ich má len informatívne), `B` je informatívne (hĺbku určujú pomenované parametre).

**Po `CreateObjects` vždy nastav `A`, `B`, `ZZYZX` (alebo `dimensions`)** – inak ich objekt zdedí z predvolieb nástroja
Objekt (posledne použitý objekt), nie zo šablóny.

## Šablóny

| Šablóna | Parametre | Geometria (lokálne) |
|---|---|---|
| `FS_Louvers` | `A` šírka, `ZZYZX` výška, `iOrient` 0 vodorovné / 1 zvislé, `iSpacing` 0 počet / 1 rozostup, `nLam`, `pitch`, `lamD` hĺbka lamely, `lamT` hrúbka, `lamAng` uhol, `offs` odsadenie od steny, `bFrame` rám, `frameW`, `frameD`, `matLam`, `matFrame` | lamely rovnomerne v poli vo vnútri rámu (stredy `lo + p/2 + i·p`, pri rozostupe vycentrované), otočené okolo vlastnej osi; rám = 4 hranoly `A × frameD × frameW` |
| `FS_SpandrelPanel` | `A`, `ZZYZX`, `thk` hrúbka, `proj` vyloženie, `nDiv` delenie, `joint` škára, `matPanel` | `nDiv` rovnakých dielov `x [0, A]`, `y [proj, proj + thk]` |
| `FS_CladdingPanels` | `A`, `ZZYZX`, `rx`, `ry` raster, `joint` škára, `thk`, `offs` odsadenie od steny, `matPanel` | raster od počiatku, škáry centrované na vnútorných čiarach rastra, vonkajšie hrany lícujú s 0 / `A`, 0 / `ZZYZX`; posledný stĺpec/riadok je zvyšok (môže byť úzky pás) |
| `FS_Surround` | `opW`, `opH` otvor, `bandW` šírka pásu, `bandD` vyloženie, `bTop`, `bBottom`, `bLeft`, `bRight`, `headOvh` presah nadpražia, `matBand` | počiatok = ľavý dolný roh otvoru; ostenia `z [0, opH]`, nadpražie nad nimi `z [opH, opH + bandW]` cez ostenia + presah, spodný pás pod ostením |

## Zmena šablóny

Uprav XML (skripty v CDATA), zvýš verziu v komentári `! FS_xxx 1.x` v master skripte a spusti `test_i4_gdl.py`.
`MainGUID` nemeň (uuid5 z mena) – umiestnené prvky sa naň viažu. Nový parameter pridaj na koniec zoznamu.
