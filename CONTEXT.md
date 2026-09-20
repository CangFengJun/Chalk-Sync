# ChalkSync

ChalkSync turns timestamped course material and visual evidence into synchronized study notes through a two-stage model pipeline.

## Language

**Worker Stage**:
The pipeline stage that turns bounded transcript segments and selected visual evidence into structured evidence bundles.
_Avoid_: Analysis model, first model

**Final Stage**:
The pipeline stage that reconciles all evidence bundles with the original transcript and produces the course notes.
_Avoid_: Summary model, second model

**Model Profile**:
A named selection of a model endpoint, model, wire protocol, and inference behavior that can be assigned independently to either pipeline stage.
_Avoid_: Provider when referring to the complete selectable configuration

**Profile Assignment**:
The choice of Model Profile for a particular pipeline stage in one run.
_Avoid_: Default model

**Artifact Provenance**:
The model profile and inference settings that identify how a generated artifact was produced.
_Avoid_: Cache metadata
