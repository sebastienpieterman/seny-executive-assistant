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
import { Plus, X, Pencil } from "lucide-react";
import { api } from "@/lib/api";
import { toast } from "sonner";

const TOTAL_STEPS = 7;

const STEP_TITLES = [
  "Welcome",
  "Your Name",
  "About You",
  "Key People",
  "Projects & Goals",
  "Optional Features",
  "Review & Complete",
];

export function SetupPage() {
  const { isAuthenticated, refreshSetupStatus } = useAuth();
  const navigate = useNavigate();
  const [currentStep, setCurrentStep] = useState(0);
  const [saving, setSaving] = useState(false);

  // Step 1 state
  const [userName, setUserName] = useState("");
  const [pronounPreset, setPronounPreset] = useState("they");
  const [pronounSubject, setPronounSubject] = useState("they");
  const [pronounObject, setPronounObject] = useState("them");
  const [pronounPossessive, setPronounPossessive] = useState("their");

  // Step 2 state
  const [userContext, setUserContext] = useState("");

  // Step 3 state
  const [keyPeople, setKeyPeople] = useState<
    Array<{ name: string; relationship: string; context: string }>
  >([{ name: "", relationship: "friend", context: "" }]);

  // Step 4 state
  const [keyProjects, setKeyProjects] = useState<
    Array<{ name: string; description: string; priority: string }>
  >([{ name: "", description: "", priority: "medium" }]);
  const [priorities, setPriorities] = useState("");

  // Step 5 state (Optional Features)
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
      if (data.user_context) setUserContext(data.user_context);
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
    } else if (currentStep === 2) {
      saved = await saveStep({ user_context: userContext });
    } else if (currentStep === 3) {
      const filtered = keyPeople.filter((p) => p.name.trim() !== "");
      saved = await saveStep({ key_people: JSON.stringify(filtered) });
    } else if (currentStep === 4) {
      const filtered = keyProjects.filter((p) => p.name.trim() !== "");
      saved = await saveStep({
        key_projects: JSON.stringify(filtered),
        priorities,
      });
    } else if (currentStep === 5) {
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

  // Render step content
  function renderStep() {
    switch (currentStep) {
      case 0:
        return (
          <div className="py-8 text-center space-y-4">
            <h2 className="text-2xl font-semibold text-foreground">
              Welcome to Seny
            </h2>
            <p className="text-muted-foreground max-w-md mx-auto">
              Let's personalize your assistant. This takes about 2 minutes.
            </p>
            <p className="text-sm text-muted-foreground">
              You can skip any step and come back later.
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

      case 2:
        return (
          <div className="space-y-2">
            <Label htmlFor="user-context">Tell Seny about yourself</Label>
            <Textarea
              id="user-context"
              rows={5}
              placeholder={`What do you do? What matters to you? What should Seny know about your life?\n\nExamples:\n- I'm a product manager at a startup, working on launching our first product. I also freelance on weekends.\n- I'm a stay-at-home parent with two kids. I'm trying to get more organized and build a side business.\n- I'm a grad student researching climate policy. I have ADHD and use tools to stay focused.`}
              value={userContext}
              onChange={(e) => setUserContext(e.target.value)}
            />
          </div>
        );

      case 3:
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

            <div className="space-y-3">
              {keyPeople.map((person, index) => (
                <div
                  key={index}
                  className="flex gap-2 items-start rounded-lg border border-border p-3"
                >
                  <div className="flex-1 grid grid-cols-1 sm:grid-cols-3 gap-2">
                    <Input
                      placeholder="Name"
                      value={person.name}
                      onChange={(e) =>
                        updatePerson(index, "name", e.target.value)
                      }
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
                    <Input
                      placeholder="Why are they important?"
                      value={person.context}
                      onChange={(e) =>
                        updatePerson(index, "context", e.target.value)
                      }
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

      case 4:
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

            <div className="space-y-3">
              {keyProjects.map((project, index) => (
                <div
                  key={index}
                  className="flex gap-2 items-start rounded-lg border border-border p-3"
                >
                  <div className="flex-1 grid grid-cols-1 sm:grid-cols-3 gap-2">
                    <Input
                      placeholder="Project name"
                      value={project.name}
                      onChange={(e) =>
                        updateProject(index, "name", e.target.value)
                      }
                    />
                    <Input
                      placeholder="What is it?"
                      value={project.description}
                      onChange={(e) =>
                        updateProject(index, "description", e.target.value)
                      }
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

      case 5:
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
                    A desktop companion that watches what you're working on and provides
                    contextual nudges. Requires installing a small desktop app.
                  </p>
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
                    Syncs your Chrome browsing history to give Seny context about what
                    you're researching. Requires a small Python script.
                  </p>
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

      case 6: {
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

            {/* About You */}
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
              <p className="text-sm whitespace-pre-wrap">
                {userContext || "Not provided"}
              </p>
            </div>

            {/* Key People */}
            <div className="rounded-lg border border-border p-4">
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-medium text-muted-foreground">
                  Key People
                </h3>
                <button
                  onClick={() => setCurrentStep(3)}
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
                  onClick={() => setCurrentStep(4)}
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
                  onClick={() => setCurrentStep(5)}
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
          {currentStep < TOTAL_STEPS - 1 && (
            <Button variant="ghost" onClick={skipStep}>
              Skip
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
