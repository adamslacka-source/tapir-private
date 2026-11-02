#pragma once

#include "CommandBase.hpp"
#include "ElementCreationCommands.hpp"
#include "ObjectState.hpp"

GS::Optional<GS::UniString> BuildMeshPolyMemoFromGeometry (
    API_Element& elem,
    API_ElementMemo& memo,
    GS::Array<GS::ObjectState>& polygonCoordinates,
    const GS::Array<GS::ObjectState>& polygonArcs,
    const GS::Array<GS::ObjectState>& holes);

void BuildMeshSublinesMemoFromGeometry (
    API_Element& elem,
    API_ElementMemo& memo,
    const GS::Array<GS::ObjectState>& sublines);

// Shared by CreateMeshes and ModifyMeshes: the layer, story visibility, cover fill, surface and pen
// settings of the Mesh. Writes the mask bits; returns an error text on an invalid value.
GS::Optional<GS::UniString> ApplyMeshSettings (
    API_Element& element,
    API_Element& mask,
    const GS::ObjectState& os,
    bool& changed);

// What Archicad did not keep of the settings in os, for the 'warnings' of a command result.
GS::Array<GS::UniString> GetBeamSettingsWarnings (const API_Guid& guid, const GS::ObjectState& os);
GS::Array<GS::UniString> GetMeshSettingsWarnings (const API_Guid& guid, const GS::ObjectState& os);

bool IsHiddenLayer (const API_AttributeIndex& layerIndex);
bool IsElementLost (const API_Guid& guid);

// The JSON schema properties of ApplyMeshSettings, without the surrounding object.
extern const char* MeshSettingsSchemaProperties;

// Shared by CreateSlabsCommand (ElementCreationCommands.cpp) and ModifySlabsCommand: applies the
// 'structureType' / 'buildingMaterialId' / 'compositeId' fields of details to element.slab.
// mask may be nullptr on create. Returns an error text on an invalid or conflicting selection.
GS::Optional<GS::UniString> ApplySlabStructure (
    API_Element& element,
    API_Element* mask,
    const GS::ObjectState& details,
    bool& changed);

class CreateWallsCommand : public CreateElementsCommandBase
{
public:
    CreateWallsCommand ();
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::ObjectState> SetTypeSpecificParameters (API_Element& element, API_ElementMemo& memo, const Stories& stories, const GS::ObjectState& parameters) const override;
};

class CreateBeamsCommand : public CreateElementsCommandBase
{
public:
    CreateBeamsCommand ();
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::ObjectState> SetTypeSpecificParameters (API_Element& element, API_ElementMemo& memo, const Stories& stories, const GS::ObjectState& parameters) const override;
    virtual GS::Optional<GS::ObjectState> ChangeAfterCreation (API_Element& element, const Stories& stories, const GS::ObjectState& parameters) const override;
    virtual GS::Array<GS::UniString> GetCreationWarnings (const API_Element& element, const GS::ObjectState& parameters) const override;
};

class CreateStairsCommand : public CreateElementsCommandBase
{
public:
    CreateStairsCommand ();
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::ObjectState> SetTypeSpecificParameters (API_Element& element, API_ElementMemo& memo, const Stories& stories, const GS::ObjectState& parameters) const override;
    virtual GS::Optional<GS::ObjectState> ChangeAfterCreation (API_Element& element, const Stories& stories, const GS::ObjectState& parameters) const override;
};

class SetStairFloorPlanDisplayCommand : public CommandBase
{
public:
    SetStairFloorPlanDisplayCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class GetStairDetailsCommand : public CommandBase
{
public:
    GetStairDetailsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class ModifyStairsCommand : public CommandBase
{
public:
    ModifyStairsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class ProbeElementAccessCommand : public CommandBase
{
public:
    ProbeElementAccessCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class GetStairBaseLineDataCommand : public CommandBase
{
public:
    GetStairBaseLineDataCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class CreateWindowsCommand : public CommandBase
{
public:
    CreateWindowsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class CreateDoorsCommand : public CommandBase
{
public:
    CreateDoorsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class CreateOpeningsCommand : public CommandBase
{
public:
    CreateOpeningsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class CreateMorphsCommand : public CommandBase
{
public:
    CreateMorphsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class CreateRoofsCommand : public CommandBase
{
public:
    CreateRoofsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class CreateAssociativeDimensionsCommand : public CommandBase
{
public:
    CreateAssociativeDimensionsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class CreateAssociativeDimensionsOnSectionCommand : public CommandBase
{
public:
    CreateAssociativeDimensionsOnSectionCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class CreateWallThicknessDimensionsCommand : public CommandBase
{
public:
    CreateWallThicknessDimensionsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class ModifyWallsCommand : public CommandBase
{
public:
    ModifyWallsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class ModifyBeamsCommand : public CommandBase
{
public:
    ModifyBeamsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class ModifySlabsCommand : public CommandBase
{
public:
    ModifySlabsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class ModifyColumnsCommand : public CommandBase
{
public:
    ModifyColumnsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class ModifyWindowsCommand : public CommandBase
{
public:
    ModifyWindowsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class ModifyMorphsCommand : public CommandBase
{
public:
    ModifyMorphsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class ModifyRoofsCommand : public CommandBase
{
public:
    ModifyRoofsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class ModifyDoorsCommand : public CommandBase
{
public:
    ModifyDoorsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class CreateSectionsCommand : public CommandBase
{
public:
    CreateSectionsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class CreateInteriorElevationsCommand : public CommandBase
{
public:
    CreateInteriorElevationsCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class GetDimensionDataCommand : public CommandBase
{
public:
    GetDimensionDataCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};

class ModifyMeshesCommand : public CommandBase
{
public:
    ModifyMeshesCommand ();
    virtual GS::String GetName () const override;
    virtual GS::Optional<GS::UniString> GetInputParametersSchema () const override;
    virtual GS::Optional<GS::UniString> GetRawResponseSchema () const override;
    virtual GS::ObjectState Execute (const GS::ObjectState& parameters, GS::ProcessControl& processControl) const override;
};
