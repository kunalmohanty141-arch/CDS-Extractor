// Parser fixture. Every construct below has, at some point, made a regex-based
// "validator" report a defect that is not there — or miss one that is.
//
//   define view ZI_NOT_REAL as select from t000 { key mandt }   <- commented out
//   group by customer                                          <- commented out
//   union all                                                  <- commented out
/* A block comment containing:
     inner join vbak on vbak.vbeln = vbap.vbeln
     sum(amount)
     distinct
   None of it is code. */
@EndUserText.label: 'Comment and literal traps'
@Analytics: { dataCategory: #DIMENSION,
  dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_COMMENT_TRAPS
  as select from tgsb
{
  key gsber                                  as BusinessArea,
      'group by is not happening here'       as Note,          // literal, not a clause
      'it''s an escaped quote'               as Escaped,
      case when gsber = 'X'
           then 'inner join'                                   // literal inside CASE
           else 'left outer to many join'
      end                                    as CaseResult,
      concat(gtext, ' // not a comment')     as Concatenated
}
