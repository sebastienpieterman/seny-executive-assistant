import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Plus, X, Pencil, ChevronDown, ChevronUp, Shield } from "lucide-react";
import { api } from "@/lib/api";
import { toast } from "sonner";

const TOTAL_STEPS = 10;

const STEP_TITLES = [
  "Welcome",
  "Your Name",
  "Work & Career",
  "Personal Life",
  "Goals & Motivation",
  "How You Operate",
  "Key People",
  "Projects & Goals",
  "Optional Features",
  "Review & Complete",
];

// Questionnaire JSON keys
interface QuestionnaireData {
  work_role: string;
  work_day: string;
  work_tools: string;
  personal_hobbies: string;
  personal_health: string;
  personal_life: string;
  goals_working: string;
  goals_motivation: string;
  goals_procrastinating: string;
  operate_best: string;
  operate_derail: string;
  operate_hard: string;
}

function PrivacyBanner() {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="w-full max-w-2xl mb-4">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 rounded-lg bg-muted/50 border border-border px-4 py-2.5 text-left transition-colors hover:bg-muted/70"
      >
        <Shield className="size-4 text-muted-foreground shrink-0" />
        <span className="text-xs text-muted-foreground flex-1">
          Your answers are private and stored only on your server.
        </span>
        {expanded ? (
          <ChevronUp className="size-4 text-muted-foreground shrink-0" />
        ) : (
          <ChevronDown className="size-4 text-muted-foreground shrink-0" />
        )}
      </button>
      {expanded && (
        <div className="rounded-b-lg border border-t-0 border-border bg-muted/30 px-4 py-3 text-xs text-muted-foreground leading-relaxed space-y-2">
          <p>
            Your answers live on your server. Seny is self-hosted — your profile
            data is stored in your own database, not ours. When you chat, your
            profile is sent to Anthropic's Claude API to personalize responses.
            Anthropic does not train on API data. No other third party ever sees
            your information.
          </p>
          <p>
            Take your time here. The more honest and detailed you are, the more
            useful Seny is from day one. Every answer you give here is context
            Seny would otherwise need weeks of conversations to learn. This is
            the single biggest shortcut to making it genuinely valuable.
          </p>
        </div>
      )}
    </div>
  );
}

export function SetupPage() {
  const { isAuthenticated, refreshSetupStatus } = useAuth();
  const navigate = useNavigate();
  const [currentStep, setCurrentStep] = useState(0);
  const [saving, setSaving] = useState(false);

  // Step 1 (Name) state
  const [userName, setUserName] = useState("");
  const [pronounPreset, setPronounPreset] = useState("they");
  const [pronounSubject, setPronounSubject] = useState("they");
  const [pronounObject, setPronounObject] = useState("them");
  const [pronounPossessive, setPronounPossessive] = useState("their");

  // Questionnaire state (steps 2-5)
  const [workRole, setWorkRole] = useState("");
  const [workDay, setWorkDay] = useState("");
  const [workTools, setWorkTools] = useState("");
  const [personalHobbies, setPersonalHobbies] = useState("");
  const [personalHealth, setPersonalHealth] = useState("");
  const [personalLife, setPersonalLife] = useState("");
  const [goalsWorking, setGoalsWorking] = useState("");
  const [goalsMotivation, setGoalsMotivation] = useState("");
  const [goalsProcrastinating, setGoalsProcrastinating] = useState("");
  const [operateBest, setOperateBest] = useState("");
  const [operateDerail, setOperateDerail] = useState("");
  const [operateHard, setOperateHard] = useState("");

  // Key People state (step 6)
  const [keyPeople, setKeyPeople] = useState<
    Array<{ name: string; relationship: string; context: string }>
  >([{ name: "", relationship: "friend", context: "" }]);

  // Projects state (step 7)
  const [keyProjects, setKeyProjects] = useState<
    Array<{ name: string; description: string; priority: string }>
  >([{ name: "", description: "", priority: "medium" }]);
  const [priorities, setPriorities] = useState("");

  // Optional Features state (step 8)
  const [screenAgentEnabled, setScreenAgentEnabled] = useState(false);
  const [browserAgentEnabled, setBrowserAgentEnabled] = useState(false);
  const [personalityCasual, setPersonalityCasual] = useState(false);

  // Redirect to login if not authenticated
  useEffect(() => {
    if (!isAuthenticated) {
      navigate("/login", { replace: true });
    }
  }, [isAuthenticated, navigate]);

  // Load profile on mount
  useEffect(() => {
    loadProfile();
  }, []);

  async function loadProfile() {
    try {
      const data = await api.get<Record<string, string>>("/api/settings/profile");
      if (data.user_name) setUserName(data.user_name);
      if (data.priorities) setPriorities(data.priorities);

      // Load pronouns
      if (data.user_pronouns_subject) {
        const subj = data.user_pronouns_subject;
        const obj = data.user_pronouns_object || "";
        const poss = data.user_pronouns_possessive || "";
        setPronounSubject(subj);
        setPronounObject(obj);
        setPronounPossessive(poss);

        // Determine preset
        if (subj === "he" && obj === "him" && poss === "his") {
          setPronounPreset("he");
        } else if (subj === "she" && obj === "her" && poss === "hers") {
          setPronounPreset("she");
        } else if (subj === "they" && obj === "them" && poss === "their") {
          setPronounPreset("they");
        } else {
          setPronounPreset("custom");
        }
      }

      // Load user_context — try JSON first, fall back to plain text
      if (data.user_context) {
        try {
          const ctx = JSON.parse(data.user_context) as QuestionnaireData;
          if (typeof ctx === "object" && ctx !== null && !Array.isArray(ctx)) {
            if (ctx.work_role) setWorkRole(ctx.work_role);
            if (ctx.work_day) setWorkDay(ctx.work_day);
            if (ctx.work_tools) setWorkTools(ctx.work_tools);
            if (ctx.personal_hobbies) setPersonalHobbies(ctx.personal_hobbies);
            if (ctx.personal_health) setPersonalHealth(ctx.personal_health);
            if (ctx.personal_life) setPersonalLife(ctx.personal_life);
            if (ctx.goals_working) setGoalsWorking(ctx.goals_working);
            if (ctx.goals_motivation) setGoalsMotivation(ctx.goals_motivation);
            if (ctx.goals_procrastinating) setGoalsProcrastinating(ctx.goals_procrastinating);
            if (ctx.operate_best) setOperateBest(ctx.operate_best);
            if (ctx.operate_derail) setOperateDerail(ctx.operate_derail);
            if (ctx.operate_hard) setOperateHard(ctx.operate_hard);
          } else {
            // Not an object — treat as legacy plain text in work_role
            setWorkRole(data.user_context);
          }
        } catch {
          // Not valid JSON — legacy plain text, put in work_role as fallback
          setWorkRole(data.user_context);
        }
      }

      // Load key people
      try {
        const people = JSON.parse(data.key_people || "[]");
        if (Array.isArray(people) && people.length > 0) {
          setKeyPeople(people);
        }
      } catch {
        // Invalid JSON, keep default
      }

      // Load key projects
      try {
        const projects = JSON.parse(data.key_projects || "[]");
        if (Array.isArray(projects) && projects.length > 0) {
          setKeyProjects(projects);
        }
      } catch {
        // Invalid JSON, keep default
      }

      // Load optional feature toggles
      if (data.screen_agent_enabled) {
        setScreenAgentEnabled(data.screen_agent_enabled === "1" || data.screen_agent_enabled === "true");
      }
      if (data.browser_agent_enabled) {
        setBrowserAgentEnabled(data.browser_agent_enabled === "1" || data.browser_agent_enabled === "true");
      }
      if (data.personality_casual) {
        setPersonalityCasual(data.personality_casual === "1" || data.personality_casual === "true" || (data.personality_casual as unknown) === true);
      }
    } catch {
      // Profile might not exist yet, that's fine
    }
  }

  function buildUserContextJSON(): string {
    return JSON.stringify({
      work_role: workRole,
      work_day: workDay,
      work_tools: workTools,
      personal_hobbies: personalHobbies,
      personal_health: personalHealth,
      personal_life: personalLife,
      goals_working: goalsWorking,
      goals_motivation: goalsMotivation,
      goals_procrastinating: goalsProcrastinating,
      operate_best: operateBest,
      operate_derail: operateDerail,
      operate_hard: operateHard,
    });
  }

  async function saveStep(fields: Record<string, string>) {
    setSaving(true);
    try {
      await api.patch("/api/settings/profile", fields);
      toast.success("Saved!");
    } catch {
      toast.error("Failed to save. Please try again.");
      setSaving(false);
      return false;
    }
    setSaving(false);
    return true;
  }

  // Step navigation
  const goNext = async () => {
    // Save data for current step before advancing
    let saved = true;

    if (currentStep === 1) {
      saved = await saveStep({
        user_name: userName,
        user_pronouns_subject: pronounSubject,
        user_pronouns_object: pronounObject,
        user_pronouns_possessive: pronounPossessive,
      });
    } else if (currentStep >= 2 && currentStep <= 5) {
      // Questionnaire steps — save all as JSON user_context
      saved = await saveStep({ user_context: buildUserContextJSON() });
    } else if (currentStep === 6) {
      const filtered = keyPeople.filter((p) => p.name.trim() !== "");
      saved = await saveStep({ key_people: JSON.stringify(filtered) });
    } else if (currentStep === 7) {
      const filtered = keyProjects.filter((p) => p.name.trim() !== "");
      saved = await saveStep({
        key_projects: JSON.stringify(filtered),
        priorities,
      });
    } else if (currentStep === 8) {
      saved = await saveStep({
        screen_agent_enabled: screenAgentEnabled ? "1" : "0",
        browser_agent_enabled: browserAgentEnabled ? "1" : "0",
        personality_casual: personalityCasual ? "1" : "0",
      });
    }

    if (saved) {
      setCurrentStep((prev) => Math.min(prev + 1, TOTAL_STEPS - 1));
    }
  };

  const goBack = () => setCurrentStep((prev) => Math.max(prev - 1, 0));
  const skipStep = () =>
    setCurrentStep((prev) => Math.min(prev + 1, TOTAL_STEPS - 1));

  const handleComplete = async () => {
    setSaving(true);
    try {
      const result = await api.post<{ success: boolean; warnings: string[] }>(
        "/api/settings/setup/complete"
      );
      if (result.warnings?.length) {
        result.warnings.forEach((w) => toast.warning(w));
      }
      toast.success("Setup complete! Welcome to Seny.");
      sessionStorage.setItem("seny_setup_just_completed", "true");
      await refreshSetupStatus();
      navigate("/", { replace: true });
    } catch {
      toast.error("Failed to complete setup. Please try again.");
    } finally {
      setSaving(false);
    }
  };

  // Pronoun preset handler
  function handlePronounPreset(value: string) {
    setPronounPreset(value);
    if (value === "he") {
      setPronounSubject("he");
      setPronounObject("him");
      setPronounPossessive("his");
    } else if (value === "she") {
      setPronounSubject("she");
      setPronounObject("her");
      setPronounPossessive("hers");
    } else if (value === "they") {
      setPronounSubject("they");
      setPronounObject("them");
      setPronounPossessive("their");
    }
    // "custom" — leave fields as-is so user can edit
  }

  // Key People helpers
  const addPerson = () =>
    setKeyPeople((prev) => [
      ...prev,
      { name: "", relationship: "friend", context: "" },
    ]);
  const removePerson = (index: number) =>
    setKeyPeople((prev) => prev.filter((_, i) => i !== index));
  const updatePerson = (index: number, field: string, value: string) => {
    setKeyPeople((prev) =>
      prev.map((p, i) => (i === index ? { ...p, [field]: value } : p)),
    );
  };

  // Key Projects helpers
  const addProject = () =>
    setKeyProjects((prev) => [
      ...prev,
      { name: "", description: "", priority: "medium" },
    ]);
  const removeProject = (index: number) =>
    setKeyProjects((prev) => prev.filter((_, i) => i !== index));
  const updateProject = (index: number, field: string, value: string) => {
    setKeyProjects((prev) =>
      prev.map((p, i) => (i === index ? { ...p, [field]: value } : p)),
    );
  };

  // Helper to check if any questionnaire field has data for review display
  function hasQuestionnaireData(): boolean {
    return !!(
      workRole || workDay || workTools ||
      personalHobbies || personalHealth || personalLife ||
      goalsWorking || goalsMotivation || goalsProcrastinating ||
      operateBest || operateDerail || operateHard
    );
  }

  // Render step content
  function renderStep() {
    switch (currentStep) {
      case 0:
        return (
          <div className="py-8 text-center space-y-4">
            <h2 className="text-2xl font-semibold text-foreground">
              Welcome to Seny
            </h2>
            <p className="text-muted-foreground max-w-lg mx-auto leading-relaxed">
              This setup is intentionally thorough — plan for about 30 minutes
              if you give thoughtful answers. That's by design. The more Seny
              knows about you up front, the faster it becomes genuinely useful in
              your day-to-day life. You can skip any section and come back later.
            </p>
          </div>
        );

      case 1:
        return (
          <div className="space-y-6">
            <div className="space-y-2">
              <Label htmlFor="user-name">What should Seny call you?</Label>
              <Input
                id="user-name"
                placeholder="e.g., Alex, Dr. Chen, Mom"
                value={userName}
                onChange={(e) => setUserName(e.target.value)}
              />
            </div>

            <div className="space-y-2">
              <Label>Your pronouns</Label>
              <Select value={pronounPreset} onValueChange={handlePronounPreset}>
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="Select pronouns" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="he">he / him / his</SelectItem>
                  <SelectItem value="she">she / her / hers</SelectItem>
                  <SelectItem value="they">they / them / their</SelectItem>
                  <SelectItem value="custom">Custom</SelectItem>
                </SelectContent>
              </Select>

              {pronounPreset === "custom" && (
                <div className="grid grid-cols-3 gap-3 mt-3">
                  <div className="space-y-1">
                    <Label htmlFor="pronoun-subject" className="text-xs text-muted-foreground">
                      Subject
                    </Label>
                    <Input
                      id="pronoun-subject"
                      placeholder="e.g., xe"
                      value={pronounSubject}
                      onChange={(e) => setPronounSubject(e.target.value)}
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="pronoun-object" className="text-xs text-muted-foreground">
                      Object
                    </Label>
                    <Input
                      id="pronoun-object"
                      placeholder="e.g., xem"
                      value={pronounObject}
                      onChange={(e) => setPronounObject(e.target.value)}
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="pronoun-possessive" className="text-xs text-muted-foreground">
                      Possessive
                    </Label>
                    <Input
                      id="pronoun-possessive"
                      placeholder="e.g., xyr"
                      value={pronounPossessive}
                      onChange={(e) => setPronounPossessive(e.target.value)}
                    />
                  </div>
                </div>
              )}
            </div>
          </div>
        );

      // Step 3: Work & Career
      case 2:
        return (
          <div className="space-y-6">
            <div className="space-y-1">
              <h2 className="text-lg font-semibold text-foreground">
                Work & Career
              </h2>
              <p className="text-sm text-muted-foreground">
                Help Seny understand what you do and how you work. All fields are optional.
              </p>
            </div>

            <div className="space-y-5">
              <div className="space-y-2">
                <Label htmlFor="work-role">
                  What do you do for work? If you wear multiple hats, describe them all.
                </Label>
                <Textarea
                  id="work-role"
                  className="min-h-[120px]"
                  placeholder="e.g., I'm a product designer at a fintech startup. I also do freelance illustration on weekends."
                  value={workRole}
                  onChange={(e) => setWorkRole(e.target.value)}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="work-day">
                  What does a typical workday actually look like for you?
                </Label>
                <Textarea
                  id="work-day"
                  className="min-h-[120px]"
                  placeholder="e.g., Mornings are meetings and Slack. Afternoons I get deep work done in Figma. I try to wrap up by 6."
                  value={workDay}
                  onChange={(e) => setWorkDay(e.target.value)}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="work-tools">
                  What tools or apps do you spend the most time in?
                </Label>
                <Textarea
                  id="work-tools"
                  className="min-h-[120px]"
                  placeholder="e.g., Figma, Slack, Gmail, Notion, VS Code"
                  value={workTools}
                  onChange={(e) => setWorkTools(e.target.value)}
                />
              </div>
            </div>
          </div>
        );

      // Step 4: Personal Life
      case 3:
        return (
          <div className="space-y-6">
            <div className="space-y-1">
              <h2 className="text-lg font-semibold text-foreground">
                Personal Life
              </h2>
              <p className="text-sm text-muted-foreground">
                The more Seny knows about you as a person, the better it can help. All fields are optional.
              </p>
            </div>

            <div className="space-y-5">
              <div className="space-y-2">
                <Label htmlFor="personal-hobbies">
                  What do you enjoy doing outside of work?
                </Label>
                <Textarea
                  id="personal-hobbies"
                  className="min-h-[120px]"
                  placeholder="e.g., Training for a half marathon, cooking Thai food, playing guitar on weekends"
                  value={personalHobbies}
                  onChange={(e) => setPersonalHobbies(e.target.value)}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="personal-health">
                  Is there anything about your health, body, or daily life that shapes how you work or what you need? This could be anything — a condition, a caregiving role, an energy pattern, or nothing at all.
                </Label>
                <Textarea
                  id="personal-health"
                  className="min-h-[120px]"
                  placeholder="e.g., I have ADHD so task initiation is hard. Once I start, I'm fine."
                  value={personalHealth}
                  onChange={(e) => setPersonalHealth(e.target.value)}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="personal-life">
                  Is there anything going on in your personal life right now that's taking up real mental space?
                </Label>
                <Textarea
                  id="personal-life"
                  className="min-h-[120px]"
                  placeholder="e.g., My partner and I are long-distance right now. Planning to move in together next year."
                  value={personalLife}
                  onChange={(e) => setPersonalLife(e.target.value)}
                />
              </div>
            </div>
          </div>
        );

      // Step 5: Goals & Motivation
      case 4:
        return (
          <div className="space-y-6">
            <div className="space-y-1">
              <h2 className="text-lg font-semibold text-foreground">
                Goals & Motivation
              </h2>
              <p className="text-sm text-muted-foreground">
                Understanding what drives you helps Seny push you in the right direction. All fields are optional.
              </p>
            </div>

            <div className="space-y-5">
              <div className="space-y-2">
                <Label htmlFor="goals-working">
                  What are you working toward right now? Think big — financially, career-wise, personally.
                </Label>
                <Textarea
                  id="goals-working"
                  className="min-h-[120px]"
                  placeholder="e.g., Trying to save enough to buy a house in the next 2 years. Also want to ship my side project."
                  value={goalsWorking}
                  onChange={(e) => setGoalsWorking(e.target.value)}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="goals-motivation">
                  When you're stuck or avoiding something, what actually gets you to move? A deadline? A person counting on you? Seeing progress? Fear of missing out?
                </Label>
                <Textarea
                  id="goals-motivation"
                  className="min-h-[120px]"
                  placeholder="e.g., Opportunity cost works on me. Reminding me what I'm leaving on the table is more effective than a task list."
                  value={goalsMotivation}
                  onChange={(e) => setGoalsMotivation(e.target.value)}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="goals-procrastinating">
                  Is there anything you've been putting off that you'd want Seny to push you on?
                </Label>
                <Textarea
                  id="goals-procrastinating"
                  className="min-h-[120px]"
                  placeholder="e.g., I need to file my taxes and I've been avoiding it for weeks."
                  value={goalsProcrastinating}
                  onChange={(e) => setGoalsProcrastinating(e.target.value)}
                />
              </div>
            </div>
          </div>
        );

      // Step 6: How You Operate
      case 5:
        return (
          <div className="space-y-6">
            <div className="space-y-1">
              <h2 className="text-lg font-semibold text-foreground">
                How You Operate
              </h2>
              <p className="text-sm text-muted-foreground">
                This helps Seny calibrate when to push, when to ease off, and how to be most useful. All fields are optional.
              </p>
            </div>

            <div className="space-y-5">
              <div className="space-y-2">
                <Label htmlFor="operate-best">
                  When are you at your best during the day? When do you hit a wall?
                </Label>
                <Textarea
                  id="operate-best"
                  className="min-h-[120px]"
                  placeholder="e.g., I'm sharpest 9am-noon. After lunch I crash. I get a second wind around 8pm."
                  value={operateBest}
                  onChange={(e) => setOperateBest(e.target.value)}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="operate-derail">
                  What tends to derail your focus or kill your momentum?
                </Label>
                <Textarea
                  id="operate-derail"
                  className="min-h-[120px]"
                  placeholder="e.g., Slack notifications, context switching between projects, getting pulled into meetings"
                  value={operateDerail}
                  onChange={(e) => setOperateDerail(e.target.value)}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="operate-hard">
                  What's genuinely hard for you that most people wouldn't know? This helps Seny know where to push and where to be patient.
                </Label>
                <Textarea
                  id="operate-hard"
                  className="min-h-[120px]"
                  placeholder="e.g., I look like I have it together but I struggle with follow-through on personal stuff."
                  value={operateHard}
                  onChange={(e) => setOperateHard(e.target.value)}
                />
              </div>
            </div>
          </div>
        );

      // Step 7: Key People
      case 6:
        return (
          <div className="space-y-4">
            <div className="space-y-1">
              <h2 className="text-lg font-semibold text-foreground">
                Who are the important people in your life?
              </h2>
              <p className="text-sm text-muted-foreground">
                Seny will check in about these relationships and help you stay
                connected.
              </p>
            </div>

            <div className="space-y-4">
              {keyPeople.map((person, index) => (
                <div
                  key={index}
                  className="flex gap-2 items-start rounded-lg border border-border p-4"
                >
                  <div className="flex-1 space-y-3">
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      <Input
                        placeholder="Name"
                        value={person.name}
                        onChange={(e) =>
                          updatePerson(index, "name", e.target.value)
                        }
                        className="sm:col-span-1"
                      />
                      <Select
                        value={person.relationship}
                        onValueChange={(v) =>
                          updatePerson(index, "relationship", v)
                        }
                      >
                        <SelectTrigger className="w-full">
                          <SelectValue placeholder="Relationship" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="partner">Partner</SelectItem>
                          <SelectItem value="family">Family</SelectItem>
                          <SelectItem value="friend">Friend</SelectItem>
                          <SelectItem value="colleague">Colleague</SelectItem>
                          <SelectItem value="mentor">Mentor</SelectItem>
                          <SelectItem value="other">Other</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    <Textarea
                      placeholder="Why are they important? What should Seny know about this relationship?"
                      value={person.context}
                      onChange={(e) =>
                        updatePerson(index, "context", e.target.value)
                      }
                      className="min-h-[80px]"
                    />
                  </div>
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    onClick={() => removePerson(index)}
                    disabled={keyPeople.length === 1}
                    className="mt-1 text-muted-foreground hover:text-destructive"
                  >
                    <X className="size-4" />
                  </Button>
                </div>
              ))}
            </div>

            <Button variant="outline" size="sm" onClick={addPerson}>
              <Plus className="size-4" />
              Add Another Person
            </Button>
          </div>
        );

      // Step 8: Projects & Goals
      case 7:
        return (
          <div className="space-y-4">
            <div className="space-y-1">
              <h2 className="text-lg font-semibold text-foreground">
                What are you working on?
              </h2>
              <p className="text-sm text-muted-foreground">
                Seny will track your projects and help you make progress.
              </p>
            </div>

            <div className="space-y-4">
              {keyProjects.map((project, index) => (
                <div
                  key={index}
                  className="flex gap-2 items-start rounded-lg border border-border p-4"
                >
                  <div className="flex-1 space-y-3">
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      <Input
                        placeholder="Project name"
                        value={project.name}
                        onChange={(e) =>
                          updateProject(index, "name", e.target.value)
                        }
                        className="sm:col-span-1"
                      />
                      <Select
                        value={project.priority}
                        onValueChange={(v) =>
                          updateProject(index, "priority", v)
                        }
                      >
                        <SelectTrigger className="w-full">
                          <SelectValue placeholder="Priority" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="high">High</SelectItem>
                          <SelectItem value="medium">Medium</SelectItem>
                          <SelectItem value="low">Low</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    <Textarea
                      placeholder="What is this project? What are you trying to accomplish?"
                      value={project.description}
                      onChange={(e) =>
                        updateProject(index, "description", e.target.value)
                      }
                      className="min-h-[80px]"
                    />
                  </div>
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    onClick={() => removeProject(index)}
                    disabled={keyProjects.length === 1}
                    className="mt-1 text-muted-foreground hover:text-destructive"
                  >
                    <X className="size-4" />
                  </Button>
                </div>
              ))}
            </div>

            <Button variant="outline" size="sm" onClick={addProject}>
              <Plus className="size-4" />
              Add Another Project
            </Button>

            <div className="space-y-2 pt-4 border-t border-border">
              <Label htmlFor="priorities">
                What matters most to you right now?
              </Label>
              <Textarea
                id="priorities"
                rows={3}
                placeholder="e.g., Getting promoted, buying a house, finishing my degree, spending more time with family"
                value={priorities}
                onChange={(e) => setPriorities(e.target.value)}
              />
            </div>
          </div>
        );

      // Step 9: Optional Features
      case 8:
        return (
          <div className="space-y-6">
            <div className="space-y-1">
              <h2 className="text-lg font-semibold text-foreground">
                Optional Features
              </h2>
              <p className="text-sm text-muted-foreground">
                These are completely optional. You can enable them later in Settings.
              </p>
            </div>

            <div className="space-y-4">
              <div className="flex items-start gap-4 rounded-lg border border-border p-4">
                <Switch
                  checked={screenAgentEnabled}
                  onCheckedChange={setScreenAgentEnabled}
                  className="mt-0.5"
                />
                <div className="flex-1">
                  <Label className="text-sm font-medium">Enable Screen Agent</Label>
                  <p className="text-sm text-muted-foreground mt-1">
                    Desktop companion that watches your screen and nudges you when you drift off-task.
                    Requires installing a small app on your computer.
                  </p>
                  <a
                    href="https://github.com/highhands89/seny-executive-assistant/blob/main/screen_agent/SETUP.md"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-primary hover:underline mt-1 inline-block"
                  >
                    View setup guide →
                  </a>
                </div>
              </div>

              <div className="flex items-start gap-4 rounded-lg border border-border p-4">
                <Switch
                  checked={browserAgentEnabled}
                  onCheckedChange={setBrowserAgentEnabled}
                  className="mt-0.5"
                />
                <div className="flex-1">
                  <Label className="text-sm font-medium">Enable Browser History Sync</Label>
                  <p className="text-sm text-muted-foreground mt-1">
                    Gives Seny context about what you've been reading in Chrome.
                    Requires running a small Python script on your computer.
                  </p>
                  <a
                    href="https://github.com/highhands89/seny-executive-assistant/blob/main/agent/README.md"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-primary hover:underline mt-1 inline-block"
                  >
                    View setup guide →
                  </a>
                </div>
              </div>

              <div className="flex items-start gap-4 rounded-lg border border-border p-4">
                <Switch
                  checked={personalityCasual}
                  onCheckedChange={setPersonalityCasual}
                  className="mt-0.5"
                />
                <div className="flex-1">
                  <Label className="text-sm font-medium">Casual Personality</Label>
                  <p className="text-sm text-muted-foreground mt-1">
                    When enabled, Seny speaks informally and may use profanity.
                    When disabled, Seny keeps it professional and clean.
                  </p>
                </div>
              </div>
            </div>
          </div>
        );

      // Step 10: Review & Complete
      case 9: {
        const filteredPeople = keyPeople.filter((p) => p.name.trim() !== "");
        const filteredProjects = keyProjects.filter((p) => p.name.trim() !== "");

        return (
          <div className="space-y-5">
            <h2 className="text-lg font-semibold text-foreground">
              Review Your Setup
            </h2>

            {/* Name & Pronouns */}
            <div className="rounded-lg border border-border p-4">
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-medium text-muted-foreground">
                  Name & Pronouns
                </h3>
                <button
                  onClick={() => setCurrentStep(1)}
                  className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
                >
                  <Pencil className="size-3" /> Edit
                </button>
              </div>
              <p className="text-sm">
                {userName || "Not provided"}
                {pronounSubject && (
                  <span className="text-muted-foreground ml-2">
                    ({pronounSubject} / {pronounObject} / {pronounPossessive})
                  </span>
                )}
              </p>
            </div>

            {/* About You (questionnaire summary) */}
            <div className="rounded-lg border border-border p-4">
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-medium text-muted-foreground">
                  About You
                </h3>
                <button
                  onClick={() => setCurrentStep(2)}
                  className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
                >
                  <Pencil className="size-3" /> Edit
                </button>
              </div>
              {hasQuestionnaireData() ? (
                <div className="space-y-1.5 text-sm">
                  {workRole && <p><span className="text-muted-foreground">Work:</span> {workRole}</p>}
                  {workDay && <p><span className="text-muted-foreground">Typical day:</span> {workDay}</p>}
                  {workTools && <p><span className="text-muted-foreground">Tools:</span> {workTools}</p>}
                  {personalHobbies && <p><span className="text-muted-foreground">Interests:</span> {personalHobbies}</p>}
                  {personalHealth && <p><span className="text-muted-foreground">Health/daily life:</span> {personalHealth}</p>}
                  {personalLife && <p><span className="text-muted-foreground">Personal context:</span> {personalLife}</p>}
                  {goalsWorking && <p><span className="text-muted-foreground">Working toward:</span> {goalsWorking}</p>}
                  {goalsMotivation && <p><span className="text-muted-foreground">What motivates:</span> {goalsMotivation}</p>}
                  {goalsProcrastinating && <p><span className="text-muted-foreground">Avoiding:</span> {goalsProcrastinating}</p>}
                  {operateBest && <p><span className="text-muted-foreground">Peak hours:</span> {operateBest}</p>}
                  {operateDerail && <p><span className="text-muted-foreground">Focus killers:</span> {operateDerail}</p>}
                  {operateHard && <p><span className="text-muted-foreground">What is hard:</span> {operateHard}</p>}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  Not provided — you can fill this in later by re-running setup
                </p>
              )}
            </div>

            {/* Key People */}
            <div className="rounded-lg border border-border p-4">
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-medium text-muted-foreground">
                  Key People
                </h3>
                <button
                  onClick={() => setCurrentStep(6)}
                  className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
                >
                  <Pencil className="size-3" /> Edit
                </button>
              </div>
              {filteredPeople.length > 0 ? (
                <ul className="space-y-1">
                  {filteredPeople.map((p, i) => (
                    <li key={i} className="text-sm">
                      <span className="font-medium">{p.name}</span>
                      <span className="text-muted-foreground">
                        {" "}({p.relationship}){p.context && ` \u2014 ${p.context}`}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted-foreground">
                  None added — you can add people later in Settings
                </p>
              )}
            </div>

            {/* Projects & Priorities */}
            <div className="rounded-lg border border-border p-4">
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-medium text-muted-foreground">
                  Projects & Goals
                </h3>
                <button
                  onClick={() => setCurrentStep(7)}
                  className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
                >
                  <Pencil className="size-3" /> Edit
                </button>
              </div>
              {filteredProjects.length > 0 ? (
                <ul className="space-y-1">
                  {filteredProjects.map((p, i) => (
                    <li key={i} className="text-sm">
                      <span className="font-medium">{p.name}</span>
                      <span className="text-muted-foreground">
                        {p.description && ` \u2014 ${p.description}`} (Priority: {p.priority})
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted-foreground">
                  None added — you can add projects later in Settings
                </p>
              )}
              {priorities && (
                <div className="mt-3 pt-3 border-t border-border">
                  <p className="text-xs font-medium text-muted-foreground mb-1">
                    What Matters Most
                  </p>
                  <p className="text-sm">{priorities}</p>
                </div>
              )}
            </div>

            {/* Optional Features */}
            <div className="rounded-lg border border-border p-4">
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-medium text-muted-foreground">
                  Optional Features
                </h3>
                <button
                  onClick={() => setCurrentStep(8)}
                  className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
                >
                  <Pencil className="size-3" /> Edit
                </button>
              </div>
              <div className="space-y-1 text-sm">
                <p>
                  Screen Agent:{" "}
                  <span className={screenAgentEnabled ? "text-green-400" : "text-muted-foreground"}>
                    {screenAgentEnabled ? "Enabled" : "Disabled"}
                  </span>
                </p>
                <p>
                  Browser History:{" "}
                  <span className={browserAgentEnabled ? "text-green-400" : "text-muted-foreground"}>
                    {browserAgentEnabled ? "Enabled" : "Disabled"}
                  </span>
                </p>
                <p>
                  Personality:{" "}
                  <span className={personalityCasual ? "text-green-400" : "text-muted-foreground"}>
                    {personalityCasual ? "Casual (may use profanity)" : "Professional (clean language)"}
                  </span>
                </p>
              </div>
            </div>
          </div>
        );
      }

      default:
        return null;
    }
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-[#0f0f0f] p-4">
      {/* Logo/brand */}
      <div className="mb-8 text-center">
        <h1 className="text-3xl font-bold text-foreground">Seny</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Let's set up your assistant
        </p>
      </div>

      {/* Progress indicator */}
      <div className="w-full max-w-2xl mb-6">
        <div className="flex justify-between text-xs text-muted-foreground mb-2">
          <span>
            Step {currentStep + 1} of {TOTAL_STEPS}
          </span>
          <span>{STEP_TITLES[currentStep]}</span>
        </div>
        <Progress value={((currentStep + 1) / TOTAL_STEPS) * 100} className="h-2" />
      </div>

      {/* Privacy banner — persistent across all steps */}
      <PrivacyBanner />

      {/* Step content */}
      <Card className="w-full max-w-2xl">
        <CardContent className="p-6">{renderStep()}</CardContent>
      </Card>

      {/* Navigation buttons */}
      <div className="w-full max-w-2xl mt-6 flex justify-between">
        <Button variant="outline" onClick={goBack} disabled={currentStep === 0}>
          Back
        </Button>
        <div className="flex gap-2">
          {currentStep > 0 && currentStep < TOTAL_STEPS - 1 && (
            <Button variant="ghost" onClick={skipStep}>
              {currentStep >= 2 && currentStep <= 5
                ? "Skip This Section"
                : "Skip"}
            </Button>
          )}
          {currentStep < TOTAL_STEPS - 1 ? (
            <Button onClick={goNext} disabled={saving}>
              {saving
                ? "Saving..."
                : currentStep === 0
                  ? "Get Started"
                  : "Next"}
            </Button>
          ) : (
            <Button onClick={handleComplete} disabled={saving}>
              {saving ? "Completing..." : "Complete Setup"}
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
