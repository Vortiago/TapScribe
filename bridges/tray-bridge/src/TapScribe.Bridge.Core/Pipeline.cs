using System.Text.Json.Serialization;

namespace TapScribe.Bridge.Core;

/// <summary>The outcome of triggering the end-of-meeting pipeline: the Recorder
/// either accepted it (202) or refused because the session already has a job in
/// flight (409). Any other non-success status surfaces as an exception.</summary>
public enum PipelineTriggerOutcome
{
    Accepted,
    Busy,
}

/// <summary>
/// A raw poll body from <c>GET /api/tap/sessions/{session}/pipeline</c>
/// (app.py <c>api_tap_pipeline_poll</c>). Which fields are present depends on
/// <see cref="State"/>; the mapper (<see cref="PipelineView.Map"/>) reads them
/// defensively. Snake-case wire names are pinned with <see cref="JsonPropertyNameAttribute"/>
/// so the contract survives serializer-option changes.
/// </summary>
public sealed record PipelinePoll
{
    [JsonPropertyName("state")] public string? State { get; init; }
    [JsonPropertyName("stage")] public string? Stage { get; init; }
    [JsonPropertyName("status")] public string? Status { get; init; }
    [JsonPropertyName("current")] public int Current { get; init; }
    [JsonPropertyName("total")] public int Total { get; init; }
    [JsonPropertyName("current_file")] public string? CurrentFile { get; init; }
    [JsonPropertyName("summary")] public PipelineSummary? Summary { get; init; }
    [JsonPropertyName("error")] public string? Error { get; init; }
    [JsonPropertyName("error_kind")] public string? ErrorKind { get; init; }
    [JsonPropertyName("started_at")] public string? StartedAt { get; init; }
    [JsonPropertyName("finished_at")] public string? FinishedAt { get; init; }
}

/// <summary>The persisted summary the Recorder returns on a <c>done</c> poll
/// (session-summary.json). <see cref="Summary"/> is the text the tray's Copy
/// button copies; <see cref="Source"/>/<see cref="Model"/> caption where it came
/// from.</summary>
public sealed record PipelineSummary
{
    [JsonPropertyName("summary")] public string? Summary { get; init; }
    [JsonPropertyName("source")] public string? Source { get; init; }
    [JsonPropertyName("model")] public string? Model { get; init; }
}
