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
