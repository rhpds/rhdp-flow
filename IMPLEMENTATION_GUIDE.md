# Flow CSV Validation for num_users Parameter Support

## Issue: RHDPOPS-24730 - Validate num_users parameter in CSV before deploy

**File:** Flow's catalog validation step (likely in `validate_sessions` or `validate_csv` endpoint)

**Add to CSV validation:**

```python
def validate_catalog_sessions(csv_data: list[dict]) -> list[dict]:
    """
    Validate that all sessions in CSV have catalog items supporting their parameters.
    
    For any row where users_override > 0:
    - Look up the CI name in the catalog
    - Check if the catalog item has 'num_users' parameter defined
    - If not, add a validation error
    """
    errors = []
    
    for row_num, row in enumerate(csv_data, start=2):  # Start at 2 (skip header)
        ci_name = row.get('ci_name')
        users_override = int(row.get('users', 0) or 0)
        
        if users_override > 0:  # Only validate if override is set
            catalog_item = get_catalog_item(ci_name)
            
            if not catalog_item:
                errors.append(f"Row {row_num}: CI '{ci_name}' not found in catalog")
                continue
            
            # Check if catalog item supports num_users parameter
            parameters = catalog_item.get('parameters', [])
            has_num_users = any(p.get('name') == 'num_users' for p in parameters)
            
            if not has_num_users:
                errors.append(
                    f"Row {row_num}: CI '{ci_name}' does not support num_users parameter. "
                    f"Users override ({users_override}) will be ignored. "
                    f"Add 'num_users' parameter to this lab's catalog definition in agnosticv."
                )
    
    if errors:
        return {
            "valid": False,
            "errors": errors,
            "message": f"CSV validation failed: {len(errors)} issue(s) found. See errors array."
        }
    
    return {"valid": True, "errors": []}
```

**Integration points:**

1. **In upload_schedule endpoint:**
   - After CSV is parsed, before stored for review
   - Return validation errors in response

2. **In deploy_session endpoint:**  
   - Run validation before sending to Babylon
   - Block deploy if any num_users mismatches found

3. **UI feedback:**
   - Show errors in the Flow modal in Labagator
   - List exact CI names and rows that fail
   - Include actionable guidance: "Add num_users to these labs in agnosticv"

**Test case:**
```
Input CSV with users=10 for a lab that has no num_users parameter
Expected: Validation fails with clear error message listing the lab and row number
```

