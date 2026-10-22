# ADR-002: Natívne schodiská ArchiCADu – podesty, zabiehanie, geometria stupňov

| Pole | Hodnota |
|---|---|
| Stav | IMPLEMENTOVANÉ, NEOVERENÉ ŽIVO (2026-10-22) – build prešiel, test čaká na reštart ArchiCADu |
| Kontext | Hotel Eden – schodiská ako nástroj Stair, nie morfy (SCH2: 66 kosých stupňov, 3 ramená, 1 podesta, 1.PP → 6.NP) |
| Kód | `archicad-addon/Sources/ExtendedElementCommands.cpp` – `CreateStairsCommand`, `GetStairBaseLineDataCommand` |
| Test | `private-tests/test_stairs_landings_winders.py` (len testovací projekt, nie Eden.pln) |

## 1. Čo dáva C API AC29 (`Support/Inc/APIdefs_Elements.h`)

| Prvok | Riadok | Obsah |
|---|---|---|
| `API_StairSegmentTypeID` | 13670 | `APIST_StepsSegment`, `APIST_LandingSegment`, `APIST_DividedLandingSegment` |
| `API_StairTurnTypeID` | 13689 | `APIST_NoTurn`, `APIST_LandingTurn`, `APIST_WinderTurn`, `APIST_DividedLandingTurn` |
| `API_StairWinderNumTypeID` | 13699 | `APIWN_Min`, `APIWN_Fix` |
| `API_StairDLWinderNumTypeID` | 13682 | `APIDWN_Max/Optimal/Fix/Uniform` |
| `API_StairWinderID` | 13748 | `APIWT_DINTurn` (pomocná vychádzková čiara), `APIWT_NZETurn` (2 body), `APIWT_SIPTurn` (1 bod), `APIWT_AGNTurn` (hlavné čiary) |
| `API_StairPolylineEdgeData` | 13820 | `type`, `geometryType`, `subElemId`, `segmentType`, `isLocked` |
| `API_StairPolylineVertexData` | 13904 | `turnType`, `winderStepNumBackward/Forward`, `numOfWindersType`, `winderType`, `innerWidthOffsetType`, `min/currLandingStart/EndEdgeOffset`, `symmetricalLandingOffset`, `dividedLanding…` |
| `API_StairPolylineData` | 14068 | `polygon`, `coords`, `pends`, `parcs`, `edgeData*`, `vertexData*` |
| `API_ElementMemo` | 18100–18112 | `stairBaseLine`, `stairWalkingLine`, `stairCustomWalkLine`, `stairCenterWalkLine`, `stairBoundary[2]` |
| `API_StairRulesData` | 14560 | min/max výška stupňa, hĺbka, 2R+G, min. dĺžka podesty, min. odsadenie vych. čiary, min. šírka zabiehania, pomer, sklon – každé s `…Applied` |
| `API_StairType` | 15644 | `totalHeight`, `topOffset`, `relativeTopStory` (15674), `flightWidth`, `stepNum`, `riserHeight`, `treadDepth`, `treadDepthLocked` (15728), `totalHeightLocked`, `walkingLinePosition/Offset`, `baselinePosition/Offset`, `rules`, `tread/riser[role]` |
| `API_LinePositionID` | 13484 | `APILP_Left/Right/Center/Auto` |
| `ACAPinc.h` | 3370 | `ACAPI_Element_Create`: Stair = „required (polyline)" |
| Príklad | `Examples/Element_Test/Src/Element_Basics.cpp:4461` | `Do_CreateStair` plní len `coords/parcs/nCoords`, edge/vertex data nie |

**NEOVERENÉ (API to nedokumentuje):** rozloženie polí `edgeData`/`vertexData` (0- vs 1-based, veľkosť), či `ACAPI_Element_Create` rešpektuje predvyplnený `turnType`, či `ACAPI_Element_Change` mení baseline memo stairu, ktorý memo mask k `stairBaseLine` patrí (žiadny `APIMemoMask_Stair…Line` neexistuje), či `relativeTopStory` stair pri vytvorení naozaj viaže. Otvor v doske API automaticky nerobí (neoverené, pravdepodobne nie).

## 2. Prečo doteraz len priame / lomené ramená

1. `edgeData/vertexData` ostávali `nullptr` (fix #444), ArchiCAD ich odvodil → typ otáčania v rohu bol vždy predvolený z nástroja.
2. Pravidlá stupňov (`rules.*Applied`, napr. 2R+G 0,60–0,65) ostali zapnuté a `treadDepthLocked` nebol nastavený → `treadDepth` ArchiCAD prepočítal („ignoruje sa").
3. Baseline = ľavý okraj, lebo `baselinePosition` sa nenastavoval.

## 3. Rozhodnutie

`CreateStairs` (spätne kompatibilné – nové polia sú voliteľné; bez nich je beh identický s doterajším):

* `turns[]` – `{pointIndex (vnútorný bod baseline, 0-based), type: None|Landing|Winder|DividedLanding, winderStepsBackward/Forward, numOfWindersType, winderType, riserOffsetType, landingStartOffset/EndOffset, symmetricalLandingOffset, dividedLanding…}`
* `segments[]` – `{edgeIndex, type: Steps|Landing|DividedLanding}`
* `lockTreadDepth`, `lockTotalHeight`, `applyRules:false` (vypne všetky pravidlá), `rules{riserHeightMin/Max, treadDepthMin/Max, twoRiserPlusGoingMin/Max, landingLengthMin, walkingLineOffsetMin, winderTurnOffsetMin}`
* `topLinkStoryOffset` + `topOffset` (väzba na podlažie), `baseLinePosition/Offset`, `walkingLinePosition/Offset`
* `turnApplyMode`: `recreate` (predvolené) | `change` (experimentálne), `keepOnTurnFailure`

Postup pri `turns/segments` (samokalibrácia namiesto hádania rozloženia polí):
1. bežný create len z `coords` (overená cesta), 2. `ACAPI_Element_GetMemo(All)` → odvodené `vertexData/edgeData`, 3. rozloženie určené z obsahu (`type == APISP_BaseLine`) a veľkosti (`BMGetPtrSize`), body spárované podľa súradníc (tolerancia 5 mm), 4. kópia baseline s upravenými `turnType/segmentType` → nový `ACAPI_Element_Create`, pôvodný stair sa zmaže až po úspechu, 5. spätné čítanie a kontrola; pri nezhode/chybe sa vráti chyba s výpisom dát a stair sa zmaže (ak nie `keepOnTurnFailure`).

Validácia: 2–64 bodov, hrany ≥ 5 cm, žiadny obrat o 180°, `stepNum` 1–400, `pointIndex` len vnútorné body, duplicity → chyba bez vytvorenia čohokoľvek. Predvolené hodnoty podprvkov sa nemenia (poučenie z `def8040`).

Nový diagnostický príkaz `GetStairBaseLineData` – baseline a vychádzková čiara presne ako ich dá API (súradnice, surové `vertexData/edgeData` s indexmi, typ otáčania/segmentu), hlavné nastavenia, pravidlá, počty stupňov (beh/podesta) a podstupnic. Slúži aj na „naučenie sa" z ručne nakresleného schodiska (`--inspect`).

## 4. Príklady

Trojramenné 6+6+6 s 2 podestami, dosky 3,10 m (os ramena, `Center`):
```json
{"stairsData":[{"baseLinePoints":[{"x":0,"y":0},{"x":0,"y":2.05},{"x":-2.65,"y":2.05},{"x":-2.65,"y":0}],
 "zCoordinate":0.0,"totalHeight":3.10,"stepNum":18,"treadDepth":0.29,"lockTreadDepth":true,"flightWidth":1.2,
 "baseLinePosition":"Center","applyRules":false,
 "turns":[{"pointIndex":1,"type":"Landing"},{"pointIndex":2,"type":"Landing"}]}]}
```
Lomené so zabiehaním:
```json
{"stairsData":[{"baseLinePoints":[{"x":10,"y":0},{"x":10,"y":3.21},{"x":12.92,"y":3.21}],
 "zCoordinate":0.0,"totalHeight":3.10,"stepNum":18,"flightWidth":1.2,"baseLinePosition":"Center",
 "turns":[{"pointIndex":1,"type":"Winder","numOfWindersType":"Min","winderType":"AuxiliaryWalkingLine"}]}]}
```

## 5. Overenie po reštarte

`python private-tests/test_stairs_landings_winders.py` v testovacom projekte; ak typ otáčania neprejde, nakresliť rukou schodisko s podestou a zabiehaním a spustiť `--inspect <guid>` – výpis ukáže skutočné rozloženie dát, podľa neho sa upraví `StairDataIndexOffset`.

## 6. Riziká

* `recreate` vytvára stair dvakrát (nový GUID) – v jednom undo kroku.
* `change` posiela celé memo (`APIMemoMask_All`) – NEOVERENÉ, môže zlyhať alebo (horší prípad) spadnúť; preto nie je predvolené.
* Počet stupňov na rameno (6+6+6) určuje ArchiCAD z geometrie baseline a dĺžky podesty – API nemá „stupne na rameno".
* Otvor v doske treba robiť zvlášť (`CreateOpenings` / úprava dosky).

## 7. Doplnok 2026-10-22 (pripomienky modelára z Edenu)

| # | Požiadavka | Čo API dáva (APIdefs_Elements.h) | Stav |
|---|---|---|---|
| 1 | posledný stupeň = doska / prvý = podlaha | Žiadne pole `lastStepAtTop`/`topStepType`/`firstStepAtBottom`/`riserNum` v `API_StairType` nie je. Jediné kandidáty: `extraTopTread` (r. 15763, „Extra Top Tread is used to display the top tread") a `extraBottomTread` (r. 15769); `treadNum` (počet stupníc, odvodený). Per-podprvok: `API_StairTreadType::visible`, `sequenceNumber`, `role` (r. 15407 ff.) – len v meme existujúceho prvku. | `extraTopTread` / `extraBottomTread` sú v `CreateStairs` a v spätnom čítaní; **význam NEOVERENÝ** – test (f) vytvorí 4 varianty a vypíše počty stupníc/podstupníc a 3D z. |
| 2 | konštrukcia 100 mm pod pätou | Pole „structure below floor / bottom offset" **neexistuje**. Je: `API_StairType::structure[role][side]` (`API_StairStructureType`, r. ~15500): `structType` (Side/Monolith/BeamSupport/Cantilevered), `bottomType` (`APISB_Stepped/Flat/Smooth`, r. 13624), `thickness`, `horizontalThickness`; monolit (`API_MonolithStairStructData`): `monolithThickness`, `bottomOversize` („size of the horizontal bottom"), `thinning`; `API_StairType::structureVisibility` (vypnutie konštrukcie). | Pridané `structure{roles, sides, structType, bottomType, thickness, horizontalThickness, monolithThickness, bottomOversize, thinning}` a `structureVisible`. Monolitické polia sa píšu len pri `structType == Monolith` (union so stringerom). Či niektoré z nich odstráni 100 mm presah, je **NEOVERENÉ** – test (e)/(g) vypíše 3D rozsah z. |
| 3 | 2h+g ≤ 650 | `API_StairRulesData` (r. 14560) má 15 pravidiel, každé s `…Applied`: výška min/max, hĺbka min/max, 2R+G (`ruleMin/Max`), dĺžka podesty, odsadenie vych. čiary, šírka zabiehania, pomer R/G, R+G, sklon. Iné pole s pravidlom v `API_StairType` nie je; favorit nesie to isté `API_StairType`, takže `applyRules:false` (aplikuje sa až po favorite) vypne aj jeho pravidlá. | `applyRules:false` vypína všetkých 15; `rules{}` doplnené o `riserGoingRatio*`, `riserPlusGoing*`, `stairPitch*`. Či ArchiCAD pri `ACAPI_Element_Create` nevynúti 2h+g aj bez pravidiel (napr. normou v nastaveniach projektu) – **NEOVERENÉ**, test (e): stupeň 340 mm pri 172 mm. |
| 4 | spätné čítanie | – | `GetStairBaseLineData` vracia aj `zCoordinate` (absolútna päta), `zTop`, `homeStoryLevel`, `floorIndex`, `baseZ`, `topLinkStoryOffset`, `topLinkStoryLevel`, `topOffset`, `totalHeight`, `riserHeight`, `treadDepth`, `stepNum`, `treadNum`, `extraTop/BottomTread`, `structureVisible`, `structureDefaults[]`, všetky pravidlá. |

## 8. Prvý živý test (2026-10-22, untitled projekt) a opravy

| Zistenie | Dôsledok / oprava |
|---|---|
| `vertexData` aj `edgeData` majú dĺžku `nCoords+1`, index 0 je nepoužitý (`Undefined`), záznamy 1..n sú `BaseLine` → 1-based ako `coords`. `StairDataIndexOffset` to určil správne. | bez zmeny |
| Predvolený `turnType` vo všetkých vrcholoch je `Landing` (aj na koncoch) – „lomené" schodiská doteraz mali v rohu podestu. | zabiehanie treba zadať výslovne (`type: Winder`) |
| Načítané súradnice baseline sa **nezhodujú** so zadanými bodmi (pri `baseLinePosition: Center` ani v tolerancii 5 mm; posun / lokálny systém NEOVERENÝ), `nCoords` je však rovnaké. Párovanie podľa súradníc preto zlyhalo → (a)–(d) FAIL. | `MapStairPoint`: ak sa počet bodov zhoduje, páruje sa podľa poradia (k = i+1); súradnice len ak ArchiCAD body pridal/zlúčil. Chybové hlásenie vypisuje aj súradnice z handle. |
| `treadDepth` sa číta vždy 1,0 (aj pri `lockTreadDepth`). Skutočná hĺbka vychádza z dĺžky baseline / počtu stupňov. | `GetStairBaseLineData` vracia `baseLineLength`, `walkingLineLength`, `goingFromRisers` (= vych. čiara / (podstupnice − 1)), `averageGoingOnWalkingLine`. Hĺbku stupňa určuje geometria baseline. |
| `applyRules:false` → 2R+G `Applied=false`; schodisko so stupňom 340 mm pri 172 mm sa vytvorilo. | bod 3 OK |
| `extraTopTread:false` → 17 stupníc / 18 podstupníc (posledná stupnica zmizne); `extraBottomTread:true` pridá 1. | bod 1 vyriešený týmito poľami |
| Predvolená konštrukcia: 3D z −0,10…3,10. `structure{Monolith, Flat, monolithThickness, bottomOversize:0}` → z 0…3,10. | bod 2 vyriešený; `API_StairType::structure` sa však číta ako 0 → `GetStairBaseLineData` vracia aj `structures[]` z umiestnených podprvkov (`memo.stairStructures`). |

## 9. Hlásenie z Edenu 2026-10-22 popoludní

| Bod | Zistenie | Riešenie |
|---|---|---|
| „Regresia" SetDetails / Move / ModifyWalls = `APIERR_BADID` (-2130313115) | Diff `def8040` (= `_prev2.apx`) → `52ed566` v `Sources/` mení len `CreateStairs`, `GetStairBaseLineData` a registráciu – žiadny spoločný helper. V untitled (port 19723, build 52ed566) `test_edit_existing_elements.py` PASS (pôdorys aj 3D okno; ručne aj okno pohľadu a layoutu). Všetky tri zlyhávajúce príkazy hľadajú prvok **vnútri `ACAPI_CallUndoableCommand`**, fungujúce (GetDetails, Get3DGeometry, GetStairBaseLineData) mimo. Príčina je v stave inštancie s Eden.pln (NEOVERENÉ: iný aktuálny databázový kontext pri undo – napr. editačný režim schodiska / iné okno architekta). | Nový read-only príkaz `ProbeElementAccess` (lookup mimo a vnútri undo, ktoré sa vráti `APIERR_CANCEL`, + aktuálna databáza a okno) – spustiť v Edene po reštarte. |
| Recreate posunie schodisko do 0,0 | Potvrdené v untitled: súradnice baseline v meme sú **lokálne k `basePlane`** (prvý bod = 0,0); recreate bral pred-create štruktúru s `basePoint` 0,0. | Šablóna = `ACAPI_Element_Get` umiestneného schodiska (správny `basePlane`), kontrola polohy 1. bodu (globálne = basePoint + x·axisX + y·axisY) a prípadná korekcia `APIEdit_Drag`. `GetStairBaseLineData` vracia `basePlane` a `baseLineGlobalCoords`. |
| `lockTreadDepth` / hĺbka | `treadDepth` sa číta vždy 1,0; ArchiCAD rozdelí dĺžku baseline na (podstupnice − 1) stupňov: priame rameno 5,78 m / 17 = 0,34 (vychádzková čiara je kratšia, 5,50 m). Pri `lockTreadDepth:false` v untitled baseline U-schodiska ostala 6,9 m (nezrútila sa). | Hĺbku určuje geometria baseline; odporúčané `lockTreadDepth:false` + `applyRules:false`. Nové pole `goingFromBaseLine`. Zrútenie v Edene pravdepodobne súviselo s posunom – overiť po reštarte (test i2). |
| Smooth monolit – päta 100 mm | Pole „päta / bottom offset" API nemá. Test (j) v untitled: predvolene z −0,100; `horizontalThickness:0`, `thinning`, `monolithThickness:0.05` bez vplyvu; **`structure.thickness:0` → z −0,032**. | Použiť `structure{structType:Monolith, bottomType:Smooth, thickness:0}` (zvyšok 32 mm pravdepodobne iná zložka – NEOVERENÉ), alebo Flat / Stepped. |
