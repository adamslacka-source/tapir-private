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

Spoločné: 2D symbol `PROJECT2 3, 270, 2` + `HOTSPOT2` na rohoch; 3D z `BLOCK` (každý prvok = 1 hranol, 8 vrcholov; `FS_ArchGable` z `PRISM_`, vrcholy 4·(nSeg+1) + výplň 2·(nSeg+1)) + `HOTSPOT`
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
| `FS_ArchGable` | `rIn`, `rOut` polomery prstenca, `depth` hĺbka do steny, `nSeg` segmenty polkruhu, `bInfill` zapustená výplň (luneta), `infRecess`, `matRing`, `matInfill` | počiatok = stred oblúka na pätke v líci steny; polkruhový prstenec `rIn..rOut` z `PRISM_` (ROTX 90), telo `y [-depth, 0]` (v stene, nie pred ňou); výplň vnútri `rIn` od `-infRecess` po `-depth`. Pilot A: oblúkový štít rizalitu M016 nad stenou zóny (pätka = vrch zóny), hlavu oblúkových dverí `Arch Double Door 27` nesie prstenec |

| `FS_Railing` | `A` dĺžka úseku, `ZZYZX` = H (vrch madla nad pätou), stĺpiky `iPostProf` 0 obdĺžnik / 1 kruh / 2 plochá oceľ, `postW` (x; Ø; hrúbka pásoviny), `postD` (y), `iPostMode` 0 počet `nPost` / 1 max. rozostup `postSp`, `pInset` os krajného stĺpika od konca, `bPostStart`, `bPostEnd`, kotvenie `iMount` 0 na doske / 1 bočné (`sideGap`, `mountDrop` presah pod pätu); madlo `bRail`, `iRailProf` 0 kruh / 1 obdĺžnik / 2 plochá oceľ, `railW` (y; Ø), `railH` (z), `railOvhS`, `railOvhE`; výplň `iInfill` 0 none / 1 horizontal_bars (`nBars`, `iBarDist` 0 rovnomerne / 1 zoznam `barZList` „0.15;0.35“ = osi nad pätou, `iBarProf`, `barD`, `barT`) / 2 vertical_balusters (`balSp` max. osový rozostup, `iBalProf`, `balD`, `bBotRail`, `botRW`, `botRH`) / 3 glass (`glassT`) / 4 panel (`panT`, `iPanType` 0 plný / 1 perforovaný = len povrch); spoločné `botGap` svetlo päta–výplň, `topGap` svetlo výplň–madlo (sklo, panel), `infGap` svetlo k stĺpiku (sklo, panel), `infOffs`; `matPost`, `matRail`, `matInfill` | y = 0 = os stĺpikov (na doske) alebo líce čela dosky (bočné, os stĺpika `sideGap + postD/2`), +y = strana pádu; stĺpiky v rovnomerných poliach medzi `pInset` a `A - pInset` (polia = `ceil(rozpätie / postSp)`), stĺpik končí pod madlom (kruhové: v osi madla); tyče a spodný pás od osi prvého po os posledného stĺpika (prechádzajú stĺpikmi); rovnomerné tyče: spodná na `botGap`, rovnaké medzery medzi tyčami a pod madlom; tyčky v každom poli rovnomerne, osový rozostup ≤ `balSp`; sklo/panel = 1 tabuľa na pole |

## FS_Railing – záloha k natívnemu zábradliu (ADR-001 R1)

API nevie zmeniť typ výplne natívneho zábradlia (`ModifyRailingDetails` mení len výšku), preto zábradlie s výplňou, ktorú
základné favority nemajú (vodorovné tyče – pilot B), je GDL objekt `FS_Railing`. Modul `facade/railing_gdl.py`
(nezávislý model geometrie, čítanie 3D po primitívach, Favorites, plán pre build), test `private-tests/test_fs_railing.py`.

- **Trasa = jeden priamy úsek na objekt.** Lomená trasa = viac objektov; spoločný vrchol nesie stĺpik len prvého úseku
  (`bPostStart`/`bPostEnd`), madlo za vnútorný vrchol pokračuje o `railW/2` (uzavretý roh). Zoznam bodov v poli parametrov
  zámerne nie: zápis polí cez `SetGDLParametersOfElements` je NEOVERENÝ, roh madla (pokos) a rozdelenie stĺpikov po
  úsekoch by bolo v GDL zložité a overenie po úsekoch je jednoduchšie.
- **Podtyp** `Railing` (vstavaný, pod *Model Element*) – `parentUnID` overené. Samotný podtyp dá v IFC `IfcBuildingElementProxy`;
  **IfcRailing** dá až klasifikácia `Zábradlie` / `Zábradlí` (`GetIFCTypeOfElements` = `IfcRailing` / `IfcRailingType`, overené
  2026-10-20). Favorites `FS rail.*` klasifikáciu nesú – prvok z Favorite je hneď `IfcRailing`.
- **Booleany** do `SetGDLParametersOfElements` posielať ako JSON `true/false` (celé číslo API odmietne).
- 3D z `Get3DGeometryOfElements` je **jedno teleso** – primitíva (`BLOCK`, `CYLIND`) sa rozlišujú ako súvislé komponenty
  polygónov (`railing_gdl.parts_of_geometry`); `RESOL 16` → valce majú krajné body presne na polomere.
- Názvy premenných GDL nerozlišujú veľkosť písmen (`px` = `pX`) – pri novom kóde pozor.

| Favorite (Object) | Obsah | Zdôvodnenie |
|---|---|---|
| `FS rail.horiz_5` | H 1,05; 5 vodorovných tyčí Ø 12 rovnomerne (spodná 0,10 nad pätou); stĺpiky plochá oceľ 60 × 12 (široká strana kolmo na zábradlie), max. á 1,2 m; madlo rúrka Ø 42 | pásovina 60 × 12 a rúrka 42,4 sú bežné prierezy oceľových zábradlí; 1,05 = výška zábradlia pilota B (ODHAD). Svetlá medzera medzi tyčami 0,17 m a vodorovné tyče („rebrík“) – **OVERIŤ NA STN 74 3305** (obytné stavby: obmedzenie svetlých otvorov a vodorovného členenia) |
| `FS rail.glass` | H 1,0; sklo 16 mm (VSG 2 × 8) 1 tabuľa na pole, svetlo k stĺpikom 15 mm, 50 mm nad pätou a pod madlom; stĺpiky 40 × 40 á max. 1,2 m; madlo Ø 42 | bežná skladba bezpečnostného skla zábradlí s madlom; hrúbka a typ skla **OVERIŤ NA STN / statikom** |
| `FS rail.balusters` | H 1,0; zvislé tyčky 12 × 12, osový rozostup ≤ 0,11 (svetlo ~0,093–0,099); spodný pás 40 × 20 na 0,05 nad pätou; stĺpiky 40 × 40 á max. 1,2; madlo Ø 42 | svetlo < 0,12 m je bežne požadované maximum pre otvory zábradlí – **OVERIŤ NA STN 74 3305** |
| `FS rail.panel_perf` | H 1,0; perforovaný plech 3 mm (perforácia = povrch, nie otvory), 1 panel na pole, svetlo 15 / 50 mm; stĺpiky 40 × 40 á max. 1,2; madlo Ø 42 | bežný plech výplní; rám panelu nemodelovaný |

Výška 1,0 m ako minimum zábradlia (pri väčšej výške pádu 1,1 m) – **OVERIŤ NA STN 74 3305**. Povrchy (`Kov - nízkouhlíková ocel`,
`Sklo - čiré rychlé`, `Kov - perforovaný malé kruhy`) sú z CZ knižnice pilotných projektov; chýbajúci povrch → 0 (povrch objektu).
Šrafa `Kov - perforovaný malé kruhy` sa v 3D náhľade vykreslí vo veľkej mierke (osemuholníky ~0,3 m) – vec povrchu, nie geometrie.

**Build** (`railing_kind: gdl` v systéme zábradlia; bez neho sa nič nemení): systém `{"type": "railing", "railing_kind": "gdl",
"favorite": "FS rail.horiz_5", "height", "post_spacing", "gdl_params": {...}}` (bez `base_favorite`); `systems.ensure` nahrá
šablónu, vyrobí štandardné Favorites (iné musia existovať) a nájde klasifikáciu; `build.plan_railings` vráti objekty
(`kind: object`, jeden na úsek, `Element ID` s príponou `.1`, `.2`, …), strana pádu = mimo plochy uzavretej trasou (balkón,
ochodza), pri priamej trase von z fasády, prípadne `railings[].fall_side`; `verify` ich číta ako objekty + porovnanie
primitív s modelom šablóny.

## Zmena šablóny

Uprav XML (skripty v CDATA), zvýš verziu v komentári `! FS_xxx 1.x` v master skripte a spusti `test_i4_gdl.py`.
`MainGUID` nemeň (uuid5 z mena) – umiestnené prvky sa naň viažu. Nový parameter pridaj na koniec zoznamu.
