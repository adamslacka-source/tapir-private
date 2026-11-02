# Mapa polí nástrojov Beam a Mesh (AC29)

| Pole | Hodnota |
|---|---|
| Stav | IMPLEMENTOVANÉ, build prešiel, **NEOVERENÉ ŽIVO** – test čaká na reštart ArchiCADu |
| Zdroj | `archicad-addon/Build/DevKits/AC29/Support/Inc/APIdefs_Elements.h`: `API_BeamType` (4563), `API_BeamSegmentType` (4388), `API_Beam_Hole` (4473), `API_AssemblySegmentData` (3709), `API_AssemblySegmentCutData` (3896), `API_AssemblySegmentSchemeData` (3820), `API_Gable` (1814), `API_MeshType` (7676), `API_MeshLevelCoord` (7622), `API_Elem_Head` (598) |
| Kód | `ApplyBeamSettings`, `ApplyMeshSettings`, `ApplyBeamSegmentsToMemo`, `CreateBeamsCommand`, `ModifyBeamsCommand` (`ExtendedElementCommands.cpp`), `CreateMeshesCommand` (`ElementCreationCommands.cpp`), `ModifyMeshesCommand`, Get: `GetDetailsOfElements` (`ElementCommands.cpp`, `AddBeamSectionFromMemo` v `CommandBase.cpp`) |
| Test | `private-tests/test_beam_mesh_full.py` (len nový súbor alebo kópia, odmietne `Kamenne.pln` a `cesty.pln`) |

Legenda: **C** = `CreateBeams` / `CreateMeshes`, **M** = `ModifyBeams` / `ModifyMeshes`, **G** = `GetDetailsOfElements` (`details`). „–" = nedá sa, dôvod v poznámke. Všetky nové parametre sú voliteľné, staré požiadavky fungujú bez zmeny.

Pri **C** sa nastavenia zapíšu do `ACAPI_Element_Create`; po vytvorení sa prvok prečíta a čo si ArchiCAD nenechal, zapíše sa ešte raz cez `ACAPI_Element_Change` s maskou (`ChangeAfterCreation`). Požiadavka bez nových polí nerobí žiadne volanie navyše.

## Hlavička prvku (`API_Elem_Head`) – Beam aj Mesh

| API | JSON | C | M | G | Poznámka |
|---|---|---|---|---|---|
| `layer` | `layerIndex` alebo `layerId` | ✓ | ✓ | ✓ (`layerIndex` na úrovni prvku) | oboje naraz = chyba; neexistujúca alebo zamknutá vrstva = chyba |
| `floorInd` | `floorIndex` | ✓ | ✓ | ✓ | |
| `renovationStatus` | `renovationStatus` | ✓ | ✓ | ✓ | `Existing`, `New`, `Demolished` |
| `drwIndex` | `drawIndex` | – | `SetDetailsOfElements` | ✓ | existujúca cesta |
| `guid`, `modiStamp`, `groupGuid`, `hotlinkGuid`, `hasMemo`, `userId`, `lockId`, `rgr_layer`, `renovationFilterGuid` | | – | – | čiastočne (`hotlinkId`) | interné alebo výstupné údaje |
| favorit (tool defaults) | `favoriteName` | ✓ | – | – | **už existoval** v oboch Create (`CreateElementsCommandBase`) |

## Beam (`API_BeamType`)

| API | JSON | C | M | G |
|---|---|---|---|---|
| `begC`, `endC` | `begCoordinate`, `endCoordinate` | ✓ | ✓ | ✓ |
| `level` | `zCoordinate` (C), `level` (M) | ✓ | ✓ | ✓ |
| `offset` | `offset` | ✓ | ✓ | ✓ |
| `slantAngle`, `isSlanted` | `slantAngle`, `isSlanted` | ✓ | ✓ | ✓ |
| `profileAngle` | `profileAngle` | ✓ | ✓ | ✓ |
| `curveAngle` | `arcAngle` | ✓ | ✓ | ✓ |
| `verticalCurveHeight` | `verticalCurveHeight` | ✓ | ✓ | ✓ |
| `beamShape` | `beamShape` | ✓ nové | ✓ | ✓ |
| `isFlipped` | `isFlipped` | ✓ nové | ✓ | ✓ |
| `anchorPoint` | `anchorPoint` | ✓ | ✓ | ✓ |
| `showReferenceAxis` | `showReferenceAxis` (`ShowAlways`, `HideAlways`, `ByMVO`) | ✓ nové | ✓ nové | ✓ nové |
| `showContourLines` | `showContourLines` (rovnako) | ✓ nové | ✓ nové | ✓ nové |
| `refPen`, `refLtype` | `referencePen`, `referenceLineTypeIndex` | ✓ nové | ✓ nové | ✓ nové |
| `cutContourLinePen`, `cutContourLineType` | `cutContourPen`, `cutContourLineTypeIndex` | ✓ nové | ✓ nové | ✓ nové |
| `belowViewLinePen`, `belowViewLineType` | `uncutLinePen`, `uncutLineTypeIndex` | ✓ nové | ✓ nové | ✓ nové |
| `aboveViewLinePen`, `aboveViewLineType` | `overheadLinePen`, `overheadLineTypeIndex` | ✓ nové | ✓ nové | ✓ nové |
| `hiddenLinePen`, `hiddenLineType` | `hiddenLinePen`, `hiddenLineTypeIndex` | ✓ nové | ✓ nové | ✓ nové |
| `displayOption` | `displayOption` | ✓ nové | ✓ nové | ✓ nové |
| `viewDepthLimitation` | `viewDepthLimitation` | ✓ nové | ✓ nové | ✓ nové |
| `uncutProjectionMode`, `overheadProjectionMode` | rovnaké mená (`Symbolic`, `Projected`, `Hybrid`) | ✓ nové | ✓ nové | ✓ nové |
| `visibility` | `visibility` (mení sa len zadaný člen) | ✓ nové | ✓ nové | ✓ nové |
| `isAutoOnStoryVisibility` | `isAutoOnStoryVisibility` | ✓ nové | ✓ nové | ✓ nové |
| `linkToSettings` | `linkToSettings` | ✓ nové | ✓ nové | ✓ nové |
| `sequence` | `sequence` | ✓ nové | ✓ nové | ✓ nové |
| `holeType`, `holeContureOn`, `holeWidth`, `holeHeight`, `holeLevel` | `holeDefaults {type, showContour, width, height, level}` | ✓ nové | ✓ nové | ✓ nové |
| `cutFillPen`, `cutFillBackgroundPen` | rovnaké mená | ✓ nové | ✓ | ✓ |
| `useCoverFill`, `useCoverFillFromSurface`, `coverFillOrientationComesFrom3D`, `coverFillType`, `coverFillForegroundPen`, `coverFillBackgroundPen`, `coverFillTransformationType`, `coverFillTransformation` | `coverFill {use, useFromSurface, orientationComesFrom3D, fillId, foregroundPen, backgroundPen, transformationType, transformation}` – od teraz aj čiastočne | ✓ nové | ✓ | ✓ |
| `nSegments`, `nCuts`, `nSchemes`, `nProfiles` | rovnaké mená | nepriamo cez `segments` | nepriamo | ✓ čítanie |

## Beam – memo

| API | JSON | C | M | G |
|---|---|---|---|---|
| `beamHoles` (`API_Beam_Hole`) | `holes [{type, showContour, centerX, centerZ, width, height}]`, `holeId` len G | ✓ nové | ✓ | ✓ |
| `gables` (`API_Gable`) | `gables [{a, b, c, d, xb, xe, nx, ny}]`; `cutElem` je len výstup API | ✓ nové | ✓ nové | ✓ nové |
| `beamSegments[]` celé pole | `segments [...]`; iný počet položiek zmení počet segmentov | ✓ nové | ✓ nové | ✓ nové |
| to isté na všetky segmenty | `allSegments {...}`, staré `width`, `height`, `circleBased`, `isWidthAndHeightLinked`, `buildingMaterialId`, `profileId` | ✓ | ✓ | prvý segment (staré polia) |
| `assemblySegmentCuts[]` | `cuts [{type: Horizontal/Vertical/Custom, customAngle}]`, presne segmenty + 1 | ✓ nové | ✓ nové | ✓ nové |
| `assemblySegmentSchemes[]` | `schemes [{lengthType: Fixed/Proportional, fixedLength, lengthProportion}]`, presne počet segmentov | ✓ nové | ✓ nové | ✓ nové |
| `assemblySegmentProfiles[]`, `customOrigProfile`, `stretchedProfile` | – | – | – | – |

Položka `BeamSegment` (`API_BeamSegmentType`): `width` (`nominalWidth`), `height` (`nominalHeight`), `isWidthAndHeightLinked`, `isHomogeneous`, `endWidth`, `endHeight`, `isEndWidthAndHeightLinked`, `circleBased`, `buildingMaterialId` / `profileId` (určia `modelElemStructureType`, `buildingMaterial`, `profileAttr`), `leftMaterial`, `topMaterial`, `rightMaterial`, `bottomMaterial`, `endsMaterial`, `extrusionMaterial`, `materialsChained`. Iba čítanie (pri zápise sa ignorujú, aby sa položka z Get dala poslať späť): `index`, `segmentId` (`head.guid`), `structureType`, `profileType`, `relativeHomeStoryLevel`.

## Mesh (`API_MeshType`)

| API | JSON | C | M | G |
|---|---|---|---|---|
| `level` | `level` | ✓ | ✓ | ✓ |
| `skirt` | `skirtType` | ✓ | ✓ | ✓ |
| `skirtLevel` | `skirtLevel` | ✓ | ✓ | ✓ |
| `smoothRidges` (nastavenie hrán, „ridge") | `ridges` (`AllSharp`, `AllSmooth`, `UserDefined`) | ✓ | ✓ | ✓ |
| `showLines` | `showLines` | ✓ | ✓ | ✓ |
| `contPen`, `levelPen`, `ltypeInd` | `contourPen`, `levelPen`, `lineTypeIndex` | ✓ | ✓ | ✓ |
| `buildingMaterial` | `buildingMaterialId` (C zruší prepisy povrchov ako doteraz, M ich nechá) | ✓ | ✓ nové | ✓ nové |
| `cutFillPen`, `cutFillBackgroundPen` | rovnaké mená | ✓ nové | ✓ nové | ✓ nové |
| `sectContPen` | `sectionContourPen` | ✓ nové | ✓ nové | ✓ nové |
| `topMat`, `sideMat`, `botMat` | `topMaterial`, `sideMaterial`, `bottomMaterial` | ✓ nové | ✓ nové | ✓ nové |
| `materialsChained` | `materialsChained` | ✓ nové | ✓ nové | ✓ nové |
| `visibilityCont` | `contourVisibility` | ✓ nové | ✓ nové | ✓ nové |
| `visibilityFill` | `fillVisibility` | ✓ nové | ✓ nové | ✓ nové |
| oboje naraz | `visibility` (skratka, len zápis) | ✓ nové | ✓ nové | – |
| `linkToSettings` | `linkToSettings` | ✓ nové | ✓ nové | ✓ nové |
| `useFloorFill`, `floorFillPen`, `floorFillBGPen`, `floorFillInd`, `useLocalOrigo`, `locOrigo`, `fillAngle`, `use3DHatching` | `coverFill {use, foregroundPen, backgroundPen, fillId, useLocalOrigin, localOrigin, angle, use3DHatching}` | ✓ nové | ✓ nové | ✓ nové |
| `poly` + `coords`, `pends`, `parcs`, `meshPolyZ` | `polygonCoordinates`, `polygonArcs`, `holes` | ✓ | ✓ | ✓ |
| `vertexIDs` | `polygonVertexIds` | – | – | ✓ nové |
| `levelLines` + `meshLevelCoords`, `meshLevelEnds` | `sublines [{coordinates}]` | ✓ | ✓ | ✓ |
| `API_MeshLevelCoord::vertexID` | `sublines[].vertexIds` (zápis ignoruje) | – | – | ✓ nové |

**V `API_MeshType` neexistuje:** `visibility` (sú dve, vyššie), `isAutoOnStoryVisibility`, `displayOption`, `viewDepthLimitation`, žiadna „roughness" ani nastavenie jednotlivých hrán. Hrany riadi len `smoothRidges` a čiary sublines.

## Filter podľa podlažia a prepnutie podlažia

| Potreba | Riešenie | Stav |
|---|---|---|
| Prvky daného podlažia bez aktívneho podlažia | nový parameter `floorIndex` v `GetElementsByType`, `GetAllElements`, `FilterElements` | nové, filtruje podľa **domovského** podlažia (`header.floorInd`). Prvok zobrazený aj na iných podlažiach cez `visibility` sa takto nenájde na tých iných; to rieši len filter `OnActualFloor` po prepnutí podlažia. |
| Prepnutie aktívneho podlažia | `ChangeWindow {"windowType": "FloorPlan", "storyIndex": n}` | **už existuje** (`APIStory_GoTo`); prepne aj okno na pôdorys, ďalší príkaz netreba. Čítanie: `GetStories.actStory`. |

## Čo sa pokryť nedá a NEOVERENÉ

Nepokryté: `assemblySegmentProfiles`, `customOrigProfile`, `stretchedProfile` sú binárne `ProfileVectorImage`, JSON nemá čo poslať; bežný profil ide cez `profileId` (atribút). `filler_*` sú rezerva.

NEOVERENÉ (dá sa overiť až po reštarte, rozhodne `test_beam_mesh_full.py`):

- `API_BeamType::visibility`: komentár API hovorí, že pri Beam táto funkcia „nebola rozšírená"; hodnota sa zapíše, ale či ju ArchiCAD drží, a či `isAutoOnStoryVisibility: true` ju neprepíše, overí test.
- `displayOption` `CutAll`, projekčný režim `Hybrid`: API ich pri nosníku neuvádza ako platné.
- Zmena počtu segmentov: príklad API `Element_Modify_ChangeParameters.cpp` to ukazuje na stĺpe, pri nosníku len zmenu segmentov a rezov.
- `gables`: zmazanie prázdnym poľom v Modify, prijatie pri Create.
- Čiary a perá pri kruhovom prierezu a pri profile (možno ich ArchiCAD ignoruje).
- `use3DHatching` je v API označené ako interné.
- Prvok vytvorený na skrytej vrstve: zápis vrstvy je podľa zadania možný; zamknutá vrstva sa odmieta vopred.
