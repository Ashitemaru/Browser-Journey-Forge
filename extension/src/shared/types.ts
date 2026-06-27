export type TraceSchemaVersion = 'journey_trace_v1';
export type RecordingMode = 'research_free_form' | 'real_user_free_form';
export type RecordingStatus =
  | 'recording'
  | 'ready'
  | 'uploading'
  | 'uploaded'
  | 'failed';

export type BrowserCapabilities = {
  browser: 'chrome' | 'firefox' | 'unknown';
  screenshots: boolean;
  video: boolean;
  webRequestBody: boolean;
  injectedResponseBody: boolean;
};

export type CaptureSettings = {
  screenshots: boolean;
  video: boolean;
  networkBodies: boolean;
  // ClawBench V2: keep raw HTTP request bodies so the V2 judge can read the
  // intercepted submission. Safe only with a fake identity bundle, so it
  // defaults on for research_free_form recordings and off otherwise.
  keepRequestBodies?: boolean;
};

export type RedactionClass =
  | 'classified_password'
  | 'classified_email'
  | 'classified_phone'
  | 'classified_address'
  | 'classified_payment'
  | 'classified_otp'
  | 'classified_token'
  | 'large_body';

export type RedactionStrategy =
  | 'raw_removed'
  | 'hashed'
  | 'classified'
  | 'truncated'
  | 'media_excluded'
  | 'body_excluded';

export type Redaction = {
  strategy: RedactionStrategy;
  classes: RedactionClass[];
  digest?: string;
  originalLength?: number;
};

export type RedactedValue<T = string> = {
  value: T | null;
  redaction?: Redaction;
};

export type TraceEnvelope = {
  schema_version: TraceSchemaVersion;
  trace_id: string;
  recording_mode: RecordingMode;
  started_at: string;
  ended_at?: string;
  label?: string;
  description?: string;
  tags: string[];
  capture_settings?: CaptureSettings;
  browser: {
    extension_version: string;
    user_agent: string;
    timezone: string;
  };
  summary: TraceSummary;
};

export type TraceSummary = {
  domains: string[];
  duration_ms: number;
  event_counts: Record<string, number>;
  screenshot_count: number;
  video_chunk_count: number;
};

export type EventBase = {
  event_id: string;
  trace_id: string;
  tab_id: number;
  timestamp: number;
  url: string;
};

export type NavigationEvent = EventBase & {
  kind: 'navigation';
  nav_type:
    | 'load'
    | 'pushState'
    | 'replaceState'
    | 'popState'
    | 'hashChange'
    | 'beforeUnload'
    | 'tabOpened'
    | 'tabClosed';
  from_url?: string;
  to_url?: string;
  opener_tab_id?: number;
};

export type ActionEvent = EventBase & {
  kind: 'action';
  action_type:
    | 'click'
    | 'dblclick'
    | 'input'
    | 'change'
    | 'submit'
    | 'keydown'
    | 'scroll'
    | 'drag'
    | 'drop'
    | 'focus'
    | 'blur'
    | 'contextmenu'
    | 'wheel'
    | 'copy'
    | 'cut'
    | 'selection'
    | 'file_select';
  target?: ElementRef;
  value?: RedactedValue;
  key?: string;
  coords?: { x: number; y: number };
  modifiers?: { ctrl?: boolean; shift?: boolean; alt?: boolean; meta?: boolean };
  wheel?: { delta_x: number; delta_y: number; delta_mode: number };
  selection?: { length: number; text?: RedactedValue };
  files?: {
    count: number;
    total_bytes: number;
    accepted_types: string[];
    selected_types: string[];
    filenames?: RedactedValue<string[]>;
  };
};

export type ElementRef = {
  tag: string;
  inputType?: string;
  id?: string;
  classes?: string[];
  role?: string;
  name?: string;
  text?: string;
  selector: string;
  xpath: string;
  rect?: { x: number; y: number; w: number; h: number };
};

export type DomSnapshotEvent = EventBase & {
  kind: 'dom_snapshot';
  trigger_event_id?: string;
  hash: string;
  nodes: DomNode[];
};

export type DomMutationSignal =
  | 'modal_added'
  | 'status_added'
  | 'list_changed'
  | 'form_control_enabled'
  | 'form_control_disabled'
  | 'node_removed';

export type DomMutationSummaryEvent = EventBase & {
  kind: 'dom_mutation_summary';
  added_nodes: number;
  removed_nodes: number;
  attribute_changes: number;
  signals: DomMutationSignal[];
  selectors: string[];
  text_samples: RedactedValue<string[]>;
};

export type DomNode = {
  ref: number;
  tag: string;
  inputType?: string;
  role?: string;
  name?: string;
  text?: RedactedValue;
  href?: string;
  value?: RedactedValue;
  frame?: FrameMetadata;
  selector: string;
  rect?: { x: number; y: number; w: number; h: number };
};

export type CaptchaProvider =
  | 'google_recaptcha'
  | 'hcaptcha'
  | 'cloudflare_turnstile'
  | 'arkose'
  | 'geetest'
  | 'generic_captcha';

export type FrameMetadata = {
  isCaptcha: boolean;
  provider?: CaptchaProvider;
  srcHost?: string;
  srcPath?: string;
  title?: string;
  name?: string;
  sandbox?: string;
};

export type NetworkRequestEvent = EventBase & {
  kind: 'network_request';
  request_id: string;
  method: string;
  full_url: string;
  initiator?: string;
  fetch_kind: 'xhr' | 'fetch' | 'beacon' | 'navigation' | 'other';
  req_headers: Record<string, RedactedValue>;
  req_body?: RedactedValue;
};

export type NetworkResponseEvent = EventBase & {
  kind: 'network_response';
  request_id: string;
  status?: number;
  content_type?: string;
  duration_ms?: number;
  res_body?: RedactedValue;
};

export type NetworkStreamEvent = EventBase & {
  kind: 'network_stream';
  stream_type: 'websocket' | 'eventsource';
  phase: 'open' | 'message' | 'close' | 'error';
  stream_id: string;
  full_url: string;
  direction?: 'incoming' | 'outgoing';
  byte_count?: number;
};

export type DownloadEvent = EventBase & {
  kind: 'download';
  download_id: number;
  phase: 'created' | 'completed' | 'interrupted';
  source_url?: string;
  mime?: string;
  total_bytes?: number;
  danger?: string;
  filename_ext?: string;
};

export type ScreenshotEvent = EventBase & {
  kind: 'screenshot';
  blob_key: string;
  trigger_event_id?: string;
  width?: number;
  height?: number;
};

export type VideoChunkEvent = EventBase & {
  kind: 'video_chunk';
  blob_key: string;
  start_timestamp: number;
  end_timestamp: number;
};

export type FormSummaryEvent = EventBase & {
  kind: 'form_summary';
  form_selector: string;
  phase: 'opened' | 'edited' | 'submitted' | 'reset';
  fields: { name: string; type: string; redactionClasses: RedactionClass[]; digest?: string }[];
};

export type AnnotationEvent = EventBase & {
  kind: 'annotation';
  annotation_type:
    | 'pause'
    | 'resume'
    | 'label_updated'
    | 'description_updated'
    | 'video_started'
    | 'video_stopped'
    | 'video_failed'
    | 'video_degraded';
  text?: string;
};

export type CapturedEvent =
  | NavigationEvent
  | ActionEvent
  | DomSnapshotEvent
  | DomMutationSummaryEvent
  | NetworkRequestEvent
  | NetworkResponseEvent
  | NetworkStreamEvent
  | DownloadEvent
  | ScreenshotEvent
  | VideoChunkEvent
  | FormSummaryEvent
  | AnnotationEvent;

export type PaymentBundle = {
  enabled: boolean;
  test_card_label?: string;
  [key: string]: string | number | boolean | null | undefined;
};

export type IdentityBundle = {
  identity_bundle_id: string;
  email: string;
  email_password: string;
  webmail_url: string;
  persona: Record<string, string>;
  payment: PaymentBundle;
  expires_at: string;
};

export type RecordingRow = {
  trace_id: string;
  status: RecordingStatus;
  envelope: TraceEnvelope;
  identity?: IdentityBundle;
  created_at: number;
  updated_at: number;
  upload_id?: string;
  last_error?: string;
};

export type BlobRow = {
  blob_key: string;
  trace_id: string;
  kind: 'screenshot' | 'video';
  data: Blob;
  created_at: number;
  sha256?: string;
  excluded_from_upload?: boolean;
  excluded_at?: number;
};

export type UploadMediaChunkMetadata = {
  blob_key: string;
  media_kind: BlobRow['kind'];
  mime_type: string;
  created_at: number;
  segment_index: number;
  segment_count: number;
  uncompressed_bytes: number;
};

export type UploadChunk = {
  index: number;
  kind: 'events' | 'media';
  sha256: string;
  bytes: number;
  uploaded: boolean;
  media?: UploadMediaChunkMetadata;
};

export type UploadManifest = {
  trace_id: string;
  upload_id?: string;
  chunks: UploadChunk[];
  finalized: boolean;
};
