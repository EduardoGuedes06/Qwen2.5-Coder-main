package report

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"text/template"
	"time"

	pkgerrors "github.com/pkg/errors"
	"github.com/wcharczuk/go-chart/v2"
	"github.com/zimmski/osutil"
	"github.com/zimmski/osutil/bytesutil"

	"github.com/symflower/eval-dev-quality/evaluate/metrics"
	"github.com/symflower/eval-dev-quality/log"
)

// Markdown holds the values for exporting a Markdown report.
type Markdown struct {
	// DateTime holds the timestamp of the evaluation.
	DateTime time.Time
	// Version holds the version of the evaluation tool.
	Version string
	// Revision holds the Git revision of the evaluation tool.
	Revision string

	// CSVPath holds the path of detailed CSV results.
	CSVPath string
	// LogPaths holds the path of detailed logs.
	LogPaths []string
	// ModelLogsPath holds the path of the model logs.
	ModelLogsPath string
	// SVGPath holds the path of the charted results.
	SVGPath string

	// AssessmentPerModel holds a collection of assessments per model.
	AssessmentPerModel AssessmentPerModel
	// TotalScore holds the total reachable score per task.
	TotalScore uint64
}

// markdownTemplateContext holds the template for a Markdown report.
type markdownTemplateContext struct {
	Markdown

	Categories        []*metrics.AssessmentCategory
	ModelsPerCategory map[*metrics.AssessmentCategory][]string
}

// ModelLogName formats a model name to match the logging structure.
func (c markdownTemplateContext) ModelLogName(modelName string) string {
	modelPath := filepath.Join(c.ModelLogsPath, log.CleanModelNameForFileSystem(modelName)) + string(os.PathSeparator)
	if !filepath.IsAbs(modelPath) {
		// Ensure we reference the models relative to the Markdown file itself.
		modelPath = "." + string(os.PathSeparator) + modelPath
	}

	if osutil.IsWindows() {
		// Markdown should be able to handle "/" for file paths.
		modelPath = strings.ReplaceAll(modelPath, "\\", "/")
	}

	return modelPath
}

// markdownTemplate holds the template for a Markdown report.
var markdownTemplate = template.Must(template.New("template-report").Parse(bytesutil.StringTrimIndentations(`
	# Evaluation from {{.DateTime.Format "2006-01-02 15:04:05"}}

	![Bar chart that categorizes all evaluated models.]({{.SVGPath}})

	This report was generated by [DevQualityEval benchmark](https://github.com/symflower/eval-dev-quality) in ` + "`" + `version {{.Version}}` + "`" + ` - ` + "`" + `revision {{.Revision}}` + "`" + `.

	## Results

	> Keep in mind that LLMs are nondeterministic. The following results just reflect a current snapshot.

	The results of all models have been divided into the following categories:
	{{ range $category := .Categories -}}
	- {{ $category.Name }}: {{ $category.Description }}
	{{ end }}
	The following sections list all models with their categories. Detailed scoring can be found [here]({{.CSVPath}}). The complete log of the evaluation with all outputs can be found here:{{ range .LogPaths }}
	- {{.}}{{ end }}

	{{ range $category := .Categories -}}
	{{ with $modelNames := index $.ModelsPerCategory $category -}}
	### Result category "{{ $category.Name }}"

	{{ $category.Description }}

	{{ range $modelName := $modelNames -}}
	- [` + "`" + `{{ $modelName }}` + "`" + `]({{ $.ModelLogName $modelName }})
	{{ end }}
	{{ end }}
	{{- end -}}
`)))

// barChartModelsPerCategoriesSVG generates a bar chart showing models per category and writes it out as an SVG.
func barChartModelsPerCategoriesSVG(writer io.Writer, categories []*metrics.AssessmentCategory, modelsPerCategory map[*metrics.AssessmentCategory][]string) (err error) {
	bars := make([]chart.Value, 0, len(categories))
	maxCount := 0
	for _, category := range categories {
		count := len(modelsPerCategory[category])
		if count > maxCount {
			maxCount = count
		}
		if count == 0 {
			continue
		}

		bars = append(bars, chart.Value{
			Label: category.Name,
			Value: float64(count),
		})
	}
	ticks := make([]chart.Tick, maxCount+1)
	for i := range ticks {
		ticks[i] = chart.Tick{
			Value: float64(i),
			Label: strconv.Itoa(i),
		}
	}
	graph := chart.BarChart{
		Title: "Models per Category",
		Bars:  bars,
		YAxis: chart.YAxis{
			Ticks: ticks,
		},

		Background: chart.Style{
			Padding: chart.Box{
				Top:    60,
				Bottom: 40,
			},
		},
		Height:   300,
		Width:    (len(bars) + 2) * 60,
		BarWidth: 60,
	}

	if err := graph.Render(chart.SVG, writer); err != nil {
		return pkgerrors.WithStack(err)
	}

	return nil
}

// format formats the markdown values in the template to the given writer.
func (m Markdown) format(writer io.Writer, markdownFileDirectoryPath string) (err error) {
	templateContext := markdownTemplateContext{
		Markdown:   m,
		Categories: metrics.AllAssessmentCategories,
	}
	templateContext.ModelsPerCategory = make(map[*metrics.AssessmentCategory][]string, len(metrics.AllAssessmentCategories))
	for model, assessment := range m.AssessmentPerModel {
		category := assessment.Category(m.TotalScore)
		templateContext.ModelsPerCategory[category] = append(templateContext.ModelsPerCategory[category], model)
	}

	svgFile, err := os.Create(filepath.Join(markdownFileDirectoryPath, m.SVGPath))
	if err != nil {
		return pkgerrors.WithStack(err)
	}
	defer func() {
		if e := svgFile.Close(); e != nil {
			e = pkgerrors.WithStack(e)
			if err == nil {
				err = e
			} else {
				err = errors.Join(err, e)
			}
		}
	}()

	if len(templateContext.AssessmentPerModel) > 0 {
		if err := barChartModelsPerCategoriesSVG(svgFile, metrics.AllAssessmentCategories, templateContext.ModelsPerCategory); err != nil {
			return pkgerrors.WithStack(err)
		}
	}

	if err := markdownTemplate.Execute(writer, templateContext); err != nil {
		return pkgerrors.WithStack(err)
	}

	return nil
}

// WriteToFile renders the Markdown to the given file.
func (m Markdown) WriteToFile(path string) (err error) {
	if err = os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return pkgerrors.WithStack(err)
	}
	file, err := os.Create(path)
	if err != nil {
		return pkgerrors.WithStack(err)
	}
	defer func() {
		if e := file.Close(); e != nil {
			e = pkgerrors.WithStack(e)
			if err == nil {
				err = e
			} else {
				err = errors.Join(err, e)
			}
		}
	}()

	if err := m.format(file, filepath.Dir(path)); err != nil {
		return pkgerrors.WithStack(err)
	}

	return nil
}
