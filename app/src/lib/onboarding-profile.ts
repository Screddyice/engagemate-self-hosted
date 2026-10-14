export type BrandAnalysis = {
  voice_summary: string;
  audience_summary: string;
  brand_identity: string;
  engagement_style: string;
  content_pillars: string[];
  conversation_topics: string[];
  search_terms: string[];
  us_audience_signals: string[];
  next_question: string;
};

export type BrandCandidate = {
  candidate_id: string;
  username: string;
  score: number;
  accepted: boolean;
  imported: boolean;
  us_signal: boolean;
  engagement_rate: number;
  reasons: string[];
};

export type OnboardingBrandProfile = {
  profile_id: string;
  ig_account_id: string;
  status: "pending" | "complete" | "failed";
  reference_urls: string[];
  answers: Record<string, string>;
  analysis: BrandAnalysis | null;
  candidates: BrandCandidate[];
  follower_audience_approved?: boolean;
};
